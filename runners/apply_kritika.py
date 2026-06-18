"""LinkedIn Easy Apply for Kritika Saraswat — fully isolated from Sachin's account.

Isolation:
  - Separate browser launch (own context, no shared cookies)
  - Own cookie file: data/cookies/kritika_linkedin_cookies.json
  - Reads profile_kritika.yaml (not the default profile.yaml)
  - Uses kritika_resume.pdf
  - Uses KRITIKA_LINKEDIN_EMAIL / KRITIKA_LINKEDIN_PASSWORD from .env

Berlin AI/ML jobs only. Easy Apply only.
"""

import asyncio
import json
import logging
import os
import random
import sys
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml
from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

from src.config import BASE_DIR
from src.form_classifier import classify_field, get_field_label, get_visible_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kritika")

# --- Kritika-specific paths (isolated) ---
KRITIKA_PROFILE = BASE_DIR / "data" / "profile_kritika.yaml"
KRITIKA_RESUME = BASE_DIR / "data" / "Kritika_AI_Resume.pdf"
if not KRITIKA_RESUME.exists():
    KRITIKA_RESUME = BASE_DIR / "data" / "kritika_resume.pdf"
# Kritika's own Q&A bank — keeps Sachin's data from ever bleeding into her forms.
KRITIKA_QA = BASE_DIR / "data" / "form_qa_kritika.yaml"
KRITIKA_COOKIES = BASE_DIR / "data" / "cookies" / "kritika_linkedin_cookies.json"
KRITIKA_COOKIES.parent.mkdir(parents=True, exist_ok=True)
SCREENSHOTS = BASE_DIR / "data" / "kritika_apply"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

# DRY RUN by default; pass --live to actually submit. Cap via --max=N (default 8).
DRY_RUN = "--live" not in sys.argv
def _arg_int(flag, default):
    for a in sys.argv:
        if a.startswith(flag + "="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return default
    return default
MAX_APPLY = _arg_int("--max", 8)

EMAIL = os.getenv("KRITIKA_LINKEDIN_EMAIL", "")
PASSWORD = os.getenv("KRITIKA_LINKEDIN_PASSWORD", "")

with open(KRITIKA_PROFILE, "r", encoding="utf-8") as f:
    PROFILE = yaml.safe_load(f)

PERSONAL = PROFILE["personal"]
FORM_ANSWERS = PROFILE.get("form_answers", {})

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def _save_cookies(cookies: list):
    KRITIKA_COOKIES.write_text(json.dumps(cookies))


def _load_cookies() -> list:
    if KRITIKA_COOKIES.exists():
        try:
            return json.loads(KRITIKA_COOKIES.read_text())
        except Exception:
            return []
    return []


def _human_delay(lo: float = 2.0, hi: float = 6.0) -> float:
    return random.uniform(lo, hi)


def kritika_cover_letter(job: dict) -> str:
    """Generate a cover letter tailored to Kritika's profile."""
    title = job.get("title", "Senior AI Engineer")
    company = job.get("company", "your company")
    desc = (job.get("description", "") or "").lower()

    lead = (
        "Currently at Sequoia, I architect a production multi-agent HR intelligence "
        "platform on LangGraph serving 10K+ employees, with sub-2s latency and MCP-style "
        "tool integration giving the LLM live access to source-of-truth APIs."
    )
    ab_inbev = (
        "At AB InBev (2022-2025), I led the global cash-flow forecasting model that "
        "delivered $96.9M realized benefit across multi-region treasury operations — "
        "shipped end-to-end inside a BaFin/SOX-class finance environment. I also built "
        "an OCR + LLM invoice-validation system processing 5K+ invoices/day."
    )

    return (
        f"Dear Hiring Manager,\n\n"
        f"With 7+ years designing and deploying production-grade GenAI and ML systems "
        f"across regulated finance and enterprise platforms, I am excited to apply for "
        f"the {title} position at {company}. My specialty is connecting LLMs (Claude, "
        f"GPT, LLaMA, Mistral) to live business systems through multi-agent orchestration, "
        f"MCP-style tool integration, RAG pipelines, and workflow automation.\n\n"
        f"{lead}\n\n"
        f"{ab_inbev} This experience maps directly onto regulated enterprise "
        f"and fintech environments.\n\n"
        f"I hold a PG in AI/ML from Texas McCombs School of Business and a B.Tech "
        f"from Amity University. With expertise in LangGraph, Weaviate, AWS/Azure, and "
        f"hands-on multi-agent system design, I am confident in driving impactful AI "
        f"initiatives at {company}. I am available within 60 days (current notice), "
        f"based in Bengaluru, and open to relocation within India or internationally.\n\n"
        f"Yours sincerely,\n{PERSONAL['name']}\n{PERSONAL['email']} | {PERSONAL.get('phone_pretty', '')}"
    )


async def linkedin_login(page) -> bool:
    """Login to Kritika's LinkedIn account."""
    if not EMAIL or not PASSWORD:
        logger.error("KRITIKA_LINKEDIN_EMAIL / KRITIKA_LINKEDIN_PASSWORD not set")
        return False

    # Try cookies first
    await page.goto("https://www.linkedin.com/feed/", timeout=30000)
    await asyncio.sleep(_human_delay(2, 4))
    if "/feed" in page.url and "login" not in page.url:
        logger.info("Already logged in (cookies)")
        return True

    logger.info("Logging in to Kritika's LinkedIn...")
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    # Generous wait for LinkedIn's JS-rendered form
    await asyncio.sleep(8)
    await page.screenshot(path=str(SCREENSHOTS / "login_page.png"))

    # Find visible email/text input (LinkedIn uses dynamic IDs with type="text")
    email_field = None
    for sel in ['input[type="text"]', 'input[type="email"]']:
        for inp in await page.query_selector_all(sel):
            try:
                if await inp.is_visible():
                    email_field = inp
                    break
            except Exception:
                continue
        if email_field:
            break
    if not email_field:
        logger.error("Email field not found")
        await page.screenshot(path=str(SCREENSHOTS / "login_no_email.png"))
        return False
    await email_field.click(force=True)
    for c in EMAIL:
        await page.keyboard.type(c, delay=random.randint(50, 150))

    pw_field = None
    for inp in await page.query_selector_all('input[type="password"]'):
        if await inp.is_visible():
            pw_field = inp
            break
    if not pw_field:
        logger.error("Password field not found")
        return False
    await pw_field.click(force=True)
    for c in PASSWORD:
        await page.keyboard.type(c, delay=random.randint(50, 150))

    # Click Sign In
    for btn in await page.query_selector_all("button"):
        if not await btn.is_visible():
            continue
        text = (await btn.inner_text()).strip().lower()
        if text in ("sign in", "log in", "submit"):
            await btn.click(force=True)
            break
    else:
        await page.keyboard.press("Enter")

    await asyncio.sleep(_human_delay(5, 8))
    await page.screenshot(path=str(SCREENSHOTS / "after_login.png"))

    if "challenge" in page.url or "checkpoint" in page.url:
        logger.warning("Security challenge — waiting 120s for manual resolution")
        for _ in range(24):
            await asyncio.sleep(5)
            if "/feed" in page.url:
                return True
        return False

    return "/feed" in page.url


async def find_easy_apply(page):
    for sel in [
        'a[aria-label^="Easy Apply"]',
        'button[aria-label^="Easy Apply"]',
        'a[aria-label*="Easy Apply"]',
        'button[aria-label*="Easy Apply"]',
        'button.jobs-apply-button',
        'button#jobs-apply-button-id',
    ]:
        for el in await page.query_selector_all(sel):
            try:
                if not await el.is_visible():
                    continue
                aria = await el.get_attribute("aria-label") or ""
                text = (await el.inner_text()).strip()
                if "filter" in aria.lower():
                    continue
                if "Easy Apply" in aria or text == "Easy Apply":
                    return el
            except Exception:
                continue
    return None


async def search_jobs(page, query: str, location: str) -> list[dict]:
    q = urllib.parse.quote(query)
    loc = urllib.parse.quote(location)
    # sortBy=DD = newest first, f_TPR=r604800 = past week
    url = f"https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}&f_AL=true&f_TPR=r604800&sortBy=DD"
    logger.info(f"Searching: '{query}' in {location}")
    # Retry once on LinkedIn slow-load so a single timeout doesn't tank the run
    for _attempt in range(2):
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if _attempt == 1:
                logger.warning(f"  goto failed twice ({str(e)[:80]}); skipping this query")
                return []
            logger.warning(f"  slow load, retrying once: {str(e)[:60]}")
            await asyncio.sleep(_human_delay(5, 10))
    await asyncio.sleep(_human_delay(5, 8))
    for _ in range(4):
        await page.keyboard.press("End")
        await asyncio.sleep(_human_delay(2, 3))

    jobs = []
    cards = await page.query_selector_all(
        "li.jobs-search-results__list-item, .job-card-container, [data-occludable-job-id]"
    )
    for card in cards[:30]:
        try:
            title_el = await card.query_selector(
                'a[class*="job-card-list__title"], .job-card-list__title, h3 a, .job-card-container__link'
            )
            title = (await title_el.inner_text()).strip().split("\n")[0] if title_el else ""
            company_el = await card.query_selector(
                '.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, h4'
            )
            company = (await company_el.inner_text()).strip() if company_el else ""

            job_id = await card.get_attribute("data-occludable-job-id")
            if not job_id:
                link_el = await card.query_selector("a[href*='/jobs/view/']")
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        job_id = href.split("/jobs/view/")[-1].split("/")[0].split("?")[0]
            if job_id and title:
                jobs.append({
                    "title": title, "company": company,
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                    "job_id": job_id,
                })
        except Exception:
            continue
    return jobs


async def apply_easy_apply(page, job: dict) -> dict:
    out = {"status": "failed", "notes": ""}
    cover = kritika_cover_letter(job)
    try:
        await page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                'button.jobs-apply-button, button#jobs-apply-button-id, a[aria-label^="Easy Apply"]',
                timeout=15000, state="visible",
            )
        except Exception:
            pass
        await asyncio.sleep(_human_delay(2, 4))

        btn = await find_easy_apply(page)
        if not btn:
            out["notes"] = "no Easy Apply button (external)"
            return out
        btn_text = (await btn.inner_text()).strip()
        if "Applied" in btn_text:
            out["status"] = "already_applied"
            return out

        await btn.click()
        await asyncio.sleep(_human_delay(3, 5))

        for step in range(14):
            # File upload (only when question asks for CV)
            try:
                question = await get_visible_question(page)
                q_lower = (question or "").lower()
                if any(k in q_lower for k in ["upload your cv", "upload cv", "upload resume", "lebenslauf"]):
                    for fi in await page.query_selector_all('input[type="file"]'):
                        try:
                            await fi.set_input_files(str(KRITIKA_RESUME))
                            await asyncio.sleep(2)
                            break
                        except Exception:
                            pass
            except Exception:
                pass

            # Default resume upload — happens automatically on Easy Apply first step
            for fi in await page.query_selector_all('input[type="file"]'):
                try:
                    fid = (await fi.get_attribute("id") or "").lower()
                    if "cover" in fid:
                        continue
                    if KRITIKA_RESUME.exists():
                        await fi.set_input_files(str(KRITIKA_RESUME))
                        await asyncio.sleep(2)
                        break
                except Exception:
                    pass

            # Classify-then-fill EVERY field with KRITIKA's Q&A bank. Unmatched
            # fields are skipped, never guessed (CLAUDE.md CRITICAL rule).
            for inp in await page.query_selector_all(
                'input[type="text"]:visible, input[type="tel"]:visible, '
                'input[type="email"]:visible, input[type="number"]:visible, input[type="url"]:visible'
            ):
                try:
                    if (await inp.input_value() or "").strip():
                        continue
                    label = await get_field_label(page, inp)
                    qa = classify_field(label, qa_path=KRITIKA_QA)
                    if not qa:
                        continue
                    if qa.get("field_type") in ("yes_no", "single_select", "long_text", "file_upload", "confirm_only"):
                        continue
                    val = qa.get("answer", "")
                    if val:
                        await inp.fill(str(val))
                except Exception:
                    continue

            # Radio groups: match the classified answer; SKIP unmatched (no default-Yes)
            for fs in await page.query_selector_all('fieldset[role="radiogroup"]:visible'):
                try:
                    legend = await fs.query_selector("legend")
                    legend_text = (await legend.inner_text()).strip() if legend else ""
                    qa = classify_field(legend_text, qa_path=KRITIKA_QA)
                    if not qa or "answer" not in qa:
                        continue  # don't guess — esp. work-authorization questions
                    want = str(qa["answer"]).strip().lower()
                    target = None
                    for opt in await fs.query_selector_all('input[type="radio"]'):
                        olabel = (await get_field_label(page, opt)).strip().lower()
                        oval = (await opt.get_attribute("value") or "").strip().lower()
                        if want == olabel or want == oval or olabel.startswith(want):
                            target = opt
                            break
                    if target and not await target.is_checked():
                        await target.click(force=True)
                        await asyncio.sleep(_human_delay(0.3, 0.7))
                except Exception:
                    continue

            # Selects: pick the option matching the classified answer, else SKIP
            for sel in await page.query_selector_all('select:visible'):
                try:
                    cur = await sel.input_value()
                    if cur and cur not in ("", "Select an option"):
                        continue
                    label = await get_field_label(page, sel)
                    qa = classify_field(label, qa_path=KRITIKA_QA)
                    if not qa or "answer" not in qa:
                        continue
                    want = str(qa["answer"]).strip().lower()
                    chosen = None
                    for opt in await sel.query_selector_all('option'):
                        if want and want in (await opt.inner_text()).strip().lower():
                            chosen = await opt.get_attribute("value")
                            break
                    if chosen is not None:
                        await sel.select_option(value=chosen)
                        await asyncio.sleep(_human_delay(0.3, 0.7))
                except Exception:
                    continue

            # Textareas: ONLY fill when classified as a cover-letter/long-text field
            for ta in await page.query_selector_all('textarea:visible'):
                try:
                    if (await ta.input_value() or "").strip():
                        continue
                    label = await get_field_label(page, ta)
                    qa = classify_field(label, qa_path=KRITIKA_QA)
                    if not qa or qa.get("field_type") != "long_text":
                        continue  # never blind-dump the cover letter
                    val = cover if qa.get("answer_type") == "cover_letter" else qa.get("answer", "")
                    if val:
                        await ta.fill(str(val)[:1800])
                except Exception:
                    pass

            # Submit
            submit_btn = await page.query_selector('button[aria-label="Submit application"]')
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(_human_delay(3, 5))
                out["status"] = "submitted"
                out["notes"] = f"submitted on step {step+1}"
                return out

            review_btn = await page.query_selector('button[aria-label="Review your application"]')
            if review_btn:
                await review_btn.click()
                await asyncio.sleep(_human_delay(2, 3))
                continue

            next_btn = await page.query_selector('button[aria-label="Continue to next step"]')
            if next_btn:
                await next_btn.click()
                await asyncio.sleep(_human_delay(2, 3))
                continue

            out["notes"] = f"stuck on step {step+1}"
            return out

        out["notes"] = "exceeded 14 steps"
    except Exception as e:
        out["notes"] = f"error: {str(e)[:120]}"
    return out


async def main():
    print("=" * 70)
    print(f"LINKEDIN EASY APPLY — KRITIKA SARASWAT")
    print(f"Email: {EMAIL}")
    print(f"Resume: {KRITIKA_RESUME.name}")
    print("=" * 70)

    # Kritika: full-time India primary, international remote secondary
    queries = [
        # === INDIA — primary target (Senior AI Eng, full-time) ===
        ("Senior AI Engineer", "Bengaluru"),
        ("Generative AI Engineer", "Bengaluru"),
        ("Machine Learning Engineer", "Bengaluru"),
        ("AI Engineer", "Bengaluru"),
        ("LLM Engineer", "Bengaluru"),
        ("Agentic AI Engineer", "Bengaluru"),
        ("Senior Data Scientist", "Bengaluru"),
        ("Senior AI Engineer", "Hyderabad"),
        ("Generative AI Engineer", "Hyderabad"),
        ("Machine Learning Engineer", "Hyderabad"),
        ("AI Engineer", "Pune"),
        ("Machine Learning Engineer", "Pune"),
        ("AI Engineer", "Mumbai"),
        ("Generative AI Engineer", "Mumbai"),
        ("AI Engineer", "Delhi"),
        ("Senior AI Engineer", "Gurugram"),
        ("AI Engineer", "Chennai"),
        ("AI Engineer", "India"),
        ("Senior AI Engineer", "India"),
        # === INDIA REMOTE ===
        ("remote AI engineer", "India"),
        ("remote machine learning engineer", "India"),
        # === INTERNATIONAL REMOTE / "other countries" ===
        ("remote AI engineer", "Worldwide"),
        ("remote machine learning engineer", "Worldwide"),
        ("remote LLM engineer", "Worldwide"),
        ("remote generative AI", "Worldwide"),
        # === SINGAPORE / DUBAI (English-speaking, sometimes sponsor) ===
        ("AI engineer", "Singapore"),
        ("AI engineer", "Dubai"),
        # === JAPAN (high visa sponsorship for tech) ===
        ("AI engineer", "Tokyo"),
        ("machine learning engineer", "Tokyo"),
        ("visa sponsorship AI engineer", "Japan"),
        ("LLM engineer", "Japan"),
        # === EU / US bonus passes ===
        ("AI engineer", "Berlin"),
        ("AI engineer", "European Union"),
        ("AI engineer", "United States"),
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        saved = _load_cookies()
        if saved:
            await context.add_cookies(saved)
            logger.info(f"Restored {len(saved)} cookies for Kritika")

        page = await context.new_page()
        if HAS_STEALTH:
            try:
                await Stealth().apply_stealth_async(page)
                logger.info("Applied stealth patches")
            except Exception:
                pass

        # Login
        ok = await linkedin_login(page)
        if not ok:
            logger.error("Login failed")
            await browser.close()
            return

        # Save cookies
        try:
            _save_cookies(await context.cookies())
        except Exception:
            pass

        # Search jobs
        all_jobs: list[dict] = []
        for q, loc in queries:
            try:
                jobs = await search_jobs(page, q, loc)
                logger.info(f"  → {len(jobs)} jobs")
                all_jobs.extend(jobs)
            except Exception as e:
                logger.warning(f"  search error: {e}")

        # Dedupe
        seen = set()
        unique = []
        for j in all_jobs:
            if j["job_id"] in seen:
                continue
            seen.add(j["job_id"])
            unique.append(j)
        logger.info(f"\nTotal unique jobs: {len(unique)}")

        # Rank by fit to Kritika's profile and DROP off-target roles. Her runner
        # had no scoring, so it was applying to "Back End Developer" etc. Keep only
        # genuine AI/ML/GenAI/Data-Science titles, best fit first.
        KW = [k.lower() for k in PROFILE.get("job_search", {}).get("keywords", [])]
        AI_TOKENS = [
            "ai engineer", "ai/ml", "ai engineering", "machine learning", "ml engineer",
            "data scientist", "data scien", "llm", "genai", "generative", "nlp",
            "deep learning", "mlops", "applied scien", "artificial intelligence",
        ]
        OFFTARGET = [
            "back end", "backend", "front end", "frontend", "typescript", "devops",
            "platform engineer", "robotics", "qa ", "sdet", "sales", "recruit",
            "android", "ios ", "full stack", "fullstack", "php", ".net", "golang",
        ]

        def fit_score(title: str) -> int:
            t = (title or "").lower()
            s = 0
            for kw in KW:
                if kw in t:
                    s += 35
                    break
            if any(tok in t for tok in AI_TOKENS):
                s += 25
            if any(o in t for o in OFFTARGET):
                s -= 50
            return s

        scored = sorted(((fit_score(j["title"]), j) for j in unique),
                        key=lambda x: x[0], reverse=True)
        relevant = [j for sc, j in scored if sc > 0]
        dropped = len(unique) - len(relevant)
        if dropped:
            print(f"  ⏭  Dropped {dropped} off-target roles (kept AI/ML/DS only)")
        # Post-throttle safe cap (see memory/linkedin_throttle_2026_05_19.md)
        targets = relevant[:MAX_APPLY]
        print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Top {len(targets)} jobs (of {len(unique)}, cap={MAX_APPLY}):\n")
        for j in targets:
            print(f"  • {j['title'][:50]:50s} @ {j['company'][:25]}")

        if DRY_RUN:
            print("\n" + "=" * 70)
            print("CLASSIFIER SELF-TEST (Kritika bank) — how screening questions resolve")
            print("=" * 70)
            for q in [
                "How many years of work experience do you have?",
                "Will you now or in the future require visa sponsorship?",
                "Are you legally authorized to work without sponsorship?",
                "What is your expected annual salary?",
                "What is your current notice period?",
                "Are you willing to relocate to Berlin?",
                "Phone number",
                "Email address",
                "What is your full name?",
                "LinkedIn profile URL",
                "Cover letter",
                "Why are you interested in this role?",
                "Have you ever been convicted of a felony?",
                "Random unmapped favourite-colour question",
            ]:
                qa = classify_field(q, qa_path=KRITIKA_QA)
                if not qa:
                    print(f"  SKIP     | {q[:50]:50s} | (blank — not guessed)")
                    continue
                ans = "<AI cover letter>" if qa.get("answer_type") == "cover_letter" else str(qa.get("answer", ""))[:44]
                print(f"  {qa.get('field_type','?')[:8]:8s} | {q[:50]:50s} | {ans}")
            print("\n" + "=" * 70)
            print(f"DRY RUN — NOTHING submitted. Resume on apply: {KRITIKA_RESUME.name}")
            print("Re-run with --live (optionally --max=N) to submit.")
            print("=" * 70)
            await browser.close()
            return

        submitted = 0
        for i, job in enumerate(targets, 1):
            print(f"\n[{i}/{len(targets)}] {job['title'][:55]} @ {job['company'][:25]}")
            out = await apply_easy_apply(page, job)
            icon = {"submitted": "✓", "already_applied": "~", "failed": "✗"}.get(out["status"], "?")
            print(f"  {icon} {out['status']}: {out['notes'][:80]}")
            if out["status"] == "submitted":
                submitted += 1

            try:
                _save_cookies(await context.cookies())
            except Exception:
                pass

            # POST-WARNING (2026-05-19): much longer human-like delays
            await asyncio.sleep(_human_delay(30, 55))

            # Cooldown every 5 apps (was 10) — longer + more frequent
            if i % 5 == 0 and i < len(targets):
                cooldown = _human_delay(120, 180)
                logger.info(f"  ⏸ Long cooldown {cooldown:.0f}s (post-warning safety)")
                await asyncio.sleep(cooldown)

        print(f"\n{'='*70}")
        print(f"KRITIKA FINAL: {submitted} REAL applications submitted")
        print(f"{'='*70}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
