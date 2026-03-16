"""Auto-apply to high-scoring jobs using browser automation."""

import asyncio
import logging
from datetime import date
from pathlib import Path

from src.config import (
    DRY_RUN, MAX_DAILY_APPLICATIONS, RESUME_PATH, load_profile, BASE_DIR,
)
from src import ai_engine, db
from src.browser import get_browser, linkedin_login, take_screenshot, _human_delay

logger = logging.getLogger(__name__)
SCREENSHOTS_DIR = BASE_DIR / "data" / "screenshots"


async def _ensure_screenshots_dir():
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


async def _apply_linkedin_easy(page, job: dict, cover_letter: str) -> bool:
    """Attempt LinkedIn Easy Apply."""
    await page.goto(job["url"])
    await asyncio.sleep(_human_delay(3, 5))

    easy_apply_btn = await page.query_selector('button.jobs-apply-button')
    if not easy_apply_btn:
        logger.warning(f"No Easy Apply button found for {job['title']}")
        return False

    await easy_apply_btn.click()
    await asyncio.sleep(_human_delay(2, 4))

    # Handle multi-step modal
    for step in range(5):  # max 5 steps
        # Check for file upload
        file_input = await page.query_selector('input[type="file"]')
        if file_input and RESUME_PATH.exists():
            await file_input.set_input_files(str(RESUME_PATH))
            await asyncio.sleep(_human_delay(1, 2))

        # Check for text areas (additional questions)
        textareas = await page.query_selector_all("textarea")
        for ta in textareas:
            placeholder = await ta.get_attribute("placeholder") or ""
            if not await ta.input_value():
                await ta.fill(cover_letter[:500])
                await asyncio.sleep(_human_delay(0.5, 1))

        # Look for submit or next button
        submit_btn = await page.query_selector('button[aria-label="Submit application"]')
        if submit_btn:
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would submit application for {job['title']}")
                return True
            await submit_btn.click()
            await asyncio.sleep(_human_delay(2, 3))
            return True

        next_btn = await page.query_selector('button[aria-label="Continue to next step"]')
        if next_btn:
            await next_btn.click()
            await asyncio.sleep(_human_delay(1, 2))
        else:
            break

    return False


async def _apply_external(page, job: dict, cover_letter: str) -> bool:
    """Best-effort application on external ATS sites."""
    await page.goto(job["url"])
    await asyncio.sleep(_human_delay(3, 6))

    profile = load_profile()
    personal = profile["personal"]

    # Try to find and fill common form fields
    field_mapping = {
        "name": personal["name"],
        "first_name": personal["name"].split()[0],
        "last_name": personal["name"].split()[-1],
        "email": personal["email"],
        "phone": personal["phone"],
        "linkedin": personal["linkedin_url"],
    }

    for field_key, value in field_mapping.items():
        selectors = [
            f'input[name*="{field_key}"]',
            f'input[id*="{field_key}"]',
            f'input[placeholder*="{field_key}"]',
        ]
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                await el.fill(value)
                await asyncio.sleep(_human_delay(0.3, 0.8))
                break

    # Upload resume
    file_input = await page.query_selector('input[type="file"]')
    if file_input and RESUME_PATH.exists():
        await file_input.set_input_files(str(RESUME_PATH))
        await asyncio.sleep(_human_delay(1, 2))

    # Fill cover letter textarea
    cover_textarea = await page.query_selector(
        'textarea[name*="cover"], textarea[id*="cover"], textarea[placeholder*="cover"]'
    )
    if cover_textarea:
        await cover_textarea.fill(cover_letter)
        await asyncio.sleep(_human_delay(0.5, 1))

    logger.info(f"Form partially filled for {job['title']} — manual review may be needed")
    return DRY_RUN  # Only count as success in dry run


async def apply_to_jobs(jobs: list[dict] | None = None):
    """Apply to provided jobs or fetch top candidates from DB."""
    await _ensure_screenshots_dir()

    if jobs is None:
        profile = load_profile()
        threshold = profile["job_search"].get("score_threshold", 70)
        max_per_run = profile["scheduler"]["jobs"]["apply"].get("max_per_run", 10)
        jobs = await db.get_jobs_to_apply(threshold, max_per_run)

    if not jobs:
        logger.info("No jobs to apply to")
        return

    applied_count = 0

    async with get_browser(headless=not DRY_RUN) as page:
        await linkedin_login(page)

        for job in jobs:
            if applied_count >= MAX_DAILY_APPLICATIONS:
                logger.info(f"Reached daily limit of {MAX_DAILY_APPLICATIONS}")
                break

            logger.info(f"Applying to: {job['title']} @ {job['company']}")

            # Generate cover letter
            cover_letter = ai_engine.generate_cover_letter(job)

            # Take pre-submit screenshot
            screenshot_path = str(
                SCREENSHOTS_DIR / f"{date.today()}_{job.get('id', 'unknown')}.png"
            )

            success = False
            if job.get("source") == "linkedin":
                success = await _apply_linkedin_easy(page, job, cover_letter)
            else:
                success = await _apply_external(page, job, cover_letter)

            try:
                await take_screenshot(page, screenshot_path)
            except Exception:
                screenshot_path = ""

            if success:
                await db.mark_applied(job["id"], cover_letter, screenshot_path)
                applied_count += 1
                logger.info(f"Successfully applied to {job['title']} @ {job['company']}")
            else:
                logger.warning(f"Failed to apply to {job['title']} @ {job['company']}")

            await asyncio.sleep(_human_delay(5, 10))

    logger.info(f"Applied to {applied_count} jobs this run")
