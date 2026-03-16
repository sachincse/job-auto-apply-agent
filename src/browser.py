"""Playwright browser automation helpers with human-like behavior."""

import asyncio
import random
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Page, Browser

from src.config import LINKEDIN_EMAIL, LINKEDIN_PASSWORD


def _human_delay(min_s: float = 2.0, max_s: float = 6.0):
    """Random delay to mimic human behavior."""
    return random.uniform(min_s, max_s)


@asynccontextmanager
async def get_browser(headless: bool = True):
    """Yield a Playwright browser instance."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def linkedin_login(page: Page):
    """Log in to LinkedIn."""
    await page.goto("https://www.linkedin.com/login")
    await asyncio.sleep(_human_delay())
    await page.fill("#username", LINKEDIN_EMAIL)
    await asyncio.sleep(_human_delay(0.5, 1.5))
    await page.fill("#password", LINKEDIN_PASSWORD)
    await asyncio.sleep(_human_delay(0.5, 1.0))
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(_human_delay(3, 5))


async def linkedin_search_jobs(page: Page, keywords: str, location: str) -> list[dict]:
    """Search LinkedIn for jobs and return results."""
    query = keywords.replace(" ", "%20")
    loc = location.replace(" ", "%20")
    url = f"https://www.linkedin.com/jobs/search/?keywords={query}&location={loc}&f_TPR=r86400"
    await page.goto(url)
    await asyncio.sleep(_human_delay(3, 6))

    jobs = []
    cards = await page.query_selector_all(".job-card-container")
    for card in cards[:25]:
        try:
            title_el = await card.query_selector(".job-card-list__title")
            company_el = await card.query_selector(".job-card-container__primary-description")
            link_el = await card.query_selector("a")
            title = await title_el.inner_text() if title_el else "Unknown"
            company = await company_el.inner_text() if company_el else "Unknown"
            href = await link_el.get_attribute("href") if link_el else ""
            jobs.append({
                "title": title.strip(),
                "company": company.strip(),
                "url": f"https://www.linkedin.com{href}" if href.startswith("/") else href,
                "source": "linkedin",
                "external_id": f"li_{href.split('/')[-2] if '/' in href else hash(title)}",
            })
        except Exception:
            continue
        await asyncio.sleep(_human_delay(0.3, 0.8))
    return jobs


async def linkedin_search_hiring_posts(page: Page, query: str) -> list[dict]:
    """Search LinkedIn for posts about hiring."""
    encoded = query.replace(" ", "%20")
    url = f"https://www.linkedin.com/search/results/content/?keywords={encoded}&sortBy=%22date_posted%22"
    await page.goto(url)
    await asyncio.sleep(_human_delay(3, 6))

    posts = []
    feed_items = await page.query_selector_all(".feed-shared-update-v2")
    for item in feed_items[:15]:
        try:
            text_el = await item.query_selector(".feed-shared-text")
            author_el = await item.query_selector(".update-components-actor__name")
            content = await text_el.inner_text() if text_el else ""
            author = await author_el.inner_text() if author_el else "Unknown"
            link_el = await item.query_selector("a[href*='/feed/update/']")
            post_url = await link_el.get_attribute("href") if link_el else ""
            posts.append({
                "content": content.strip(),
                "author": author.strip(),
                "url": post_url,
            })
        except Exception:
            continue
        await asyncio.sleep(_human_delay(0.3, 0.8))
    return posts


async def take_screenshot(page: Page, path: str):
    """Save a screenshot for audit trail."""
    await page.screenshot(path=path, full_page=False)
