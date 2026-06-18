"""Playwright browser automation helpers with human-like behavior + anti-detection."""

import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from pathlib import Path

# patchright (drop-in Playwright replacement) bypasses Cloudflare Turnstile by removing
# CDP `Runtime.enable` leak. DISABLED in this env — patchright's launch causes DNS resolution
# to fail (ERR_NAME_NOT_RESOLVED) even with channel=chrome. Re-enable by setting USE_PATCHRIGHT=1.
import os
if os.getenv("USE_PATCHRIGHT") == "1":
    try:
        from patchright.async_api import async_playwright
        USING_PATCHRIGHT = True
    except ImportError:
        from playwright.async_api import async_playwright
        USING_PATCHRIGHT = False
else:
    from playwright.async_api import async_playwright
    USING_PATCHRIGHT = False

from playwright.async_api import Page, Browser  # type-only

# playwright-stealth (installed) — patches all detection vectors
try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

from src.config import LINKEDIN_EMAIL, LINKEDIN_PASSWORD, BASE_DIR

logger = logging.getLogger(__name__)

# Realistic recent Chrome user agents (rotated)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

# Persist cookies so LinkedIn doesn't see a new device every run
COOKIES_PATH = BASE_DIR / "data" / "linkedin_cookies.json"


def _human_delay(min_s: float = 2.0, max_s: float = 6.0) -> float:
    """Random delay to mimic human behavior."""
    return random.uniform(min_s, max_s)


def _save_cookies(cookies: list[dict]):
    """Save browser cookies to file for session persistence."""
    with open(COOKIES_PATH, "w") as f:
        json.dump(cookies, f)


def _load_cookies() -> list[dict]:
    """Load saved cookies if they exist."""
    if COOKIES_PATH.exists():
        try:
            with open(COOKIES_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return []


@asynccontextmanager
async def get_browser(headless: bool = True):
    """Yield a Playwright browser page hardened with full anti-detection patches.

    Patches applied:
      1. Disable AutomationControlled flag
      2. Disable infobars / blink detection
      3. Random viewport sizes (1366x768 / 1920x1080 / 1536x864)
      4. Random recent Chrome User-Agent
      5. Realistic locale/timezone
      6. playwright-stealth: patches navigator.webdriver, chrome.runtime,
         missing plugins, mismatched WebGL/canvas, language fingerprints, etc.
      7. Spoof navigator.languages, plugins, hardwareConcurrency
      8. Save and reuse session cookies (avoid 'new device' login challenges)
    """
    async with async_playwright() as pw:
        # Anti-detection launch args (per claude-skills browser-automation reference).
        # NOTE: --no-sandbox removed — it's a known headless-detection signal.
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-features=IsolateOrigins,site-per-process",
            "--start-maximized",
        ]

        # Try installed Chrome first (biggest stealth win — different binary than bundled chromium)
        try:
            browser = await pw.chromium.launch(
                channel="chrome",
                headless=headless,
                args=launch_args,
            )
            tech = "installed Chrome (channel=chrome)" + (" + patchright" if USING_PATCHRIGHT else " + playwright")
            logger.info(f"Browser: {tech}")
        except Exception as e:
            logger.warning(f"Chrome channel unavailable ({str(e)[:80]}); falling back to bundled Chromium")
            browser = await pw.chromium.launch(
                headless=headless,
                args=launch_args,
            )

        # Random viewport (1920x1080 is the most common real-world resolution)
        viewport = random.choice([
            {"width": 1920, "height": 1080},
            {"width": 1366, "height": 768},
            {"width": 1536, "height": 864},
            {"width": 1440, "height": 900},
        ])
        ua = random.choice(USER_AGENTS)

        # Timezone: use Berlin (we're applying to German jobs); a German timezone
        # paired with Indian-IP creates inconsistency but is closer to "expected" for the role.
        context = await browser.new_context(
            viewport=viewport,
            screen=viewport,
            user_agent=ua,
            locale="en-US",
            timezone_id="Europe/Berlin",
            color_scheme="light",
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            permissions=["geolocation"],
        )

        # Restore saved cookies
        saved_cookies = _load_cookies()
        if saved_cookies:
            await context.add_cookies(saved_cookies)
            logger.info("Restored saved LinkedIn session cookies")

        # Patch 7: Comprehensive fingerprint spoofing init script
        await context.add_init_script("""
            // Hide webdriver
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

            // Spoof languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });

            // Spoof plugins (real Chrome has 3-5 plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    {name: 'PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer'},
                ]
            });

            // Spoof hardware
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

            // Fix Chrome runtime detection
            window.chrome = {runtime: {}, app: {}};

            // Fix permissions API for notifications
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : originalQuery(parameters)
            );

            // Patch WebGL fingerprint
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.apply(this, arguments);
            };
        """)

        page = await context.new_page()

        # Patch 6: Apply playwright-stealth (handles many additional fingerprints)
        if HAS_STEALTH:
            try:
                stealth = Stealth()
                await stealth.apply_stealth_async(page)
                logger.info("Applied playwright-stealth fingerprint patches")
            except Exception as e:
                logger.debug(f"Stealth apply failed (non-fatal): {e}")

        try:
            yield page
        finally:
            try:
                cookies = await context.cookies()
                _save_cookies(cookies)
            except Exception:
                pass
            await browser.close()


async def linkedin_login(page: Page) -> bool:
    """Log in to LinkedIn. Returns True if login succeeded."""
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        logger.warning("LinkedIn credentials not configured, skipping login")
        return False

    # Check if already logged in (from saved cookies).
    # Use domcontentloaded (default "load" waits for ads/tracking — flaky on LinkedIn).
    # Retry once on timeout because LinkedIn occasionally hangs hard.
    for attempt in range(2):
        try:
            await page.goto("https://www.linkedin.com/feed/", timeout=45000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if attempt == 1:
                logger.warning(f"LinkedIn /feed goto failed twice: {str(e)[:120]}")
                return False
            logger.warning(f"LinkedIn /feed slow ({str(e)[:60]}), retrying...")
            await asyncio.sleep(5)
    await asyncio.sleep(_human_delay(2, 4))

    if "/feed" in page.url and "login" not in page.url:
        logger.info("Already logged in to LinkedIn (session restored)")
        return True

    # Need to login
    logger.info("Logging in to LinkedIn...")
    await page.goto("https://www.linkedin.com/login")
    await asyncio.sleep(_human_delay(2, 4))

    # Find visible email field (LinkedIn uses dynamic IDs — find by visibility)
    email_field = None
    text_inputs = await page.query_selector_all('input[type="text"]')
    for inp in text_inputs:
        if await inp.is_visible():
            email_field = inp
            break

    if not email_field:
        logger.error("Could not find email field on LinkedIn login page")
        return False

    # Type email slowly like a human
    await email_field.click()
    await asyncio.sleep(_human_delay(0.3, 0.6))
    await email_field.fill("")  # Clear any existing text
    for char in LINKEDIN_EMAIL:
        await page.keyboard.type(char, delay=random.randint(50, 150))
    await asyncio.sleep(_human_delay(0.5, 1.0))

    # Find visible password field
    password_field = None
    pw_inputs = await page.query_selector_all('input[type="password"]')
    for inp in pw_inputs:
        if await inp.is_visible():
            password_field = inp
            break

    if not password_field:
        logger.error("Could not find password field on LinkedIn login page")
        return False

    await password_field.click()
    await asyncio.sleep(_human_delay(0.3, 0.6))
    for char in LINKEDIN_PASSWORD:
        await page.keyboard.type(char, delay=random.randint(50, 150))
    await asyncio.sleep(_human_delay(0.5, 1.0))

    # Click sign in — find visible button or use Enter key
    signed_in = False
    buttons = await page.query_selector_all('button')
    for btn in buttons:
        if await btn.is_visible():
            text = (await btn.inner_text()).strip().lower()
            if text in ("sign in", "log in", "submit"):
                await btn.click()
                signed_in = True
                break

    if not signed_in:
        # Fallback: press Enter from password field
        await page.keyboard.press("Enter")

    await asyncio.sleep(_human_delay(5, 8))

    # Check for security challenge / CAPTCHA
    current_url = page.url
    if "challenge" in current_url or "checkpoint" in current_url:
        logger.warning(
            "LinkedIn security challenge detected! "
            "Please complete the verification manually in the browser."
        )
        # Wait up to 120 seconds for user to solve the challenge
        for _ in range(24):
            await asyncio.sleep(5)
            if "/feed" in page.url:
                logger.info("Security challenge resolved!")
                return True
        logger.error("Security challenge not resolved within 2 minutes")
        return False

    if "/feed" in page.url:
        logger.info("LinkedIn login successful")
        return True

    logger.warning(f"LinkedIn login may have failed. Current URL: {current_url}")
    return False


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
            location_el = await card.query_selector(".job-card-container__metadata-item")
            loc = await location_el.inner_text() if location_el else location
            jobs.append({
                "title": title.strip(),
                "company": company.strip(),
                "location": loc.strip() if loc else location,
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
