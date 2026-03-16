"""Search for jobs across multiple platforms."""

import asyncio
import hashlib
import logging

import aiohttp

from src.config import (
    ADZUNA_APP_ID, ADZUNA_APP_KEY, ADZUNA_COUNTRY, load_profile,
)
from src import ai_engine, db
from src.browser import get_browser, linkedin_login, linkedin_search_jobs

logger = logging.getLogger(__name__)


async def search_adzuna(keywords: list[str], location: str, salary_min: int) -> list[dict]:
    """Search Adzuna API for jobs."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        logger.warning("Adzuna credentials not configured, skipping")
        return []

    jobs = []
    query = " ".join(keywords[:3])
    url = (
        f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/1"
        f"?app_id={ADZUNA_APP_ID}&app_key={ADZUNA_APP_KEY}"
        f"&what={query}&where={location}"
        f"&salary_min={salary_min}&results_per_page=25"
        f"&content-type=application/json"
    )
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Adzuna API error: {resp.status}")
                    return []
                data = await resp.json()
                for item in data.get("results", []):
                    jobs.append({
                        "external_id": f"az_{item.get('id', '')}",
                        "title": item.get("title", ""),
                        "company": item.get("company", {}).get("display_name", "Unknown"),
                        "location": item.get("location", {}).get("display_name", ""),
                        "url": item.get("redirect_url", ""),
                        "source": "adzuna",
                        "salary_min": item.get("salary_min"),
                        "salary_max": item.get("salary_max"),
                        "description": item.get("description", ""),
                    })
        except Exception as e:
            logger.error(f"Adzuna search failed: {e}")
    return jobs


async def search_remotive(keywords: list[str]) -> list[dict]:
    """Search Remotive API for remote jobs."""
    jobs = []
    query = "+".join(keywords[:2])
    url = f"https://remotive.com/api/remote-jobs?search={query}&limit=25"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Remotive API error: {resp.status}")
                    return []
                data = await resp.json()
                for item in data.get("jobs", []):
                    jobs.append({
                        "external_id": f"rm_{item.get('id', '')}",
                        "title": item.get("title", ""),
                        "company": item.get("company_name", "Unknown"),
                        "location": item.get("candidate_required_location", "Remote"),
                        "url": item.get("url", ""),
                        "source": "remotive",
                        "description": item.get("description", ""),
                    })
        except Exception as e:
            logger.error(f"Remotive search failed: {e}")
    return jobs


async def search_linkedin(keywords: list[str], location: str) -> list[dict]:
    """Search LinkedIn for jobs using browser automation."""
    try:
        async with get_browser() as page:
            await linkedin_login(page)
            query = " ".join(keywords[:2])
            return await linkedin_search_jobs(page, query, location)
    except Exception as e:
        logger.error(f"LinkedIn search failed: {e}")
        return []


async def search_all_platforms() -> list[dict]:
    """Search all enabled platforms, score jobs, and store in DB."""
    profile = load_profile()
    search_cfg = profile["job_search"]
    keywords = search_cfg["keywords"]
    location = search_cfg.get("location", "Remote")
    salary_min = search_cfg.get("salary_min", 0)
    platforms = search_cfg.get("platforms", {})

    tasks = []
    if platforms.get("adzuna"):
        tasks.append(search_adzuna(keywords, location, salary_min))
    if platforms.get("remotive"):
        tasks.append(search_remotive(keywords))
    if platforms.get("linkedin"):
        tasks.append(search_linkedin(keywords, location))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs = []
    for result in results:
        if isinstance(result, list):
            all_jobs.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"Platform search error: {result}")

    # Deduplicate and score
    new_jobs = []
    for job in all_jobs:
        if await db.job_exists(job["external_id"]):
            continue
        score = ai_engine.score_job(
            job["title"], job["company"], job.get("description", "")
        )
        job["fit_score"] = score
        await db.insert_job(job)
        new_jobs.append(job)
        logger.info(f"[{job['source']}] {job['title']} @ {job['company']} — Score: {score}")

    logger.info(f"Found {len(new_jobs)} new jobs across all platforms")
    return new_jobs
