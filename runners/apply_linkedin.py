"""LinkedIn Easy Apply + connection requests to hiring managers."""

import asyncio
import json
import logging
import sys
# Make src importable when run as a script from anywhere
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import db, ai_engine
from src.browser import get_browser, linkedin_login, take_screenshot, _human_delay
from src.config import RESUME_PATH, BASE_DIR, MAX_DAILY_APPLICATIONS
from src.form_classifier import classify_field, get_field_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("linkedin")

SCREENSHOTS = BASE_DIR / "data" / "linkedin_apply"
SCREENSHOTS.mkdir(exist_ok=True, parents=True)

# --- Run configuration -------------------------------------------------------
# DRY RUN by default: search + list jobs + self-test the classifier, but submit
# NOTHING. Pass --live to actually submit applications.
DRY_RUN = "--live" not in sys.argv
# Post-throttle safe rate (see memory/linkedin_throttle_2026_05_19.md): 5-8/day.
def _arg_int(flag, default):
    for a in sys.argv:
        if a.startswith(flag + "="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return default
    return default
MAX_APPLY = _arg_int("--max", 8)
# Recruiter-friendly filename (shown to recruiters on Easy Apply upload).
RESUME_FOR_RUN = BASE_DIR / "data" / "Sachin_AI_Resume.pdf"
if not RESUME_FOR_RUN.exists():
    RESUME_FOR_RUN = BASE_DIR / "data" / "Sachin_Singh_Resume.pdf"
if not RESUME_FOR_RUN.exists():
    RESUME_FOR_RUN = RESUME_PATH

# LinkedIn limits (Premium account):
# - ~200 connection requests/week
# - Connection note: 200 chars (free) / 500 chars (Premium)
DAILY_CONNECTION_LIMIT = 15  # ~100/week to stay under LinkedIn weekly limit


def generate_connect_note(name: str, role: str) -> str:
    """300-char personalized note emphasizing FIT for the role."""
    first = (name.split()[0] if name else "there").strip()
    r = role.lower()
    # Pick a fit-skill matching the role
    if any(k in r for k in ["genai", "generative", "llm"]):
        skill = "GenAI/LLM systems"
    elif any(k in r for k in ["mlops", "platform", "infrastructure"]):
        skill = "MLOps + ML platform"
    elif any(k in r for k in ["vision", "cv"]):
        skill = "computer vision"
    elif "nlp" in r:
        skill = "NLP"
    elif any(k in r for k in ["data eng", "etl"]):
        skill = "data engineering"
    elif "research" in r:
        skill = "applied ML research"
    else:
        skill = "ML/AI engineering"
    role_short = role[:32]
    note = (
        f"Hi {first}, I just applied for the {role_short} role and would love to connect. "
        f"I'm a Senior ML Engineer (8 yrs) at TrueBalance, strong in {skill}. "
        f"Open to relocate to Berlin (visa sponsorship). Excited about the fit. — Sachin"
    )
    if len(note) > 295:
        note = note[:292] + "..."
    return note


# Kept for backward compat
CONNECT_NOTE = "{generated_at_runtime}"


async def find_easy_apply_button(page):
    """Look for the Easy Apply element — could be button OR anchor link."""
    # LinkedIn renders Easy Apply differently across views:
    # - Search page card click: <button class="jobs-apply-button">Easy Apply</button>
    # - Direct /jobs/view/{id}/ URL: <a aria-label="Easy Apply to this job">Easy Apply</a>
    selectors = [
        'a[aria-label^="Easy Apply"]',
        'button[aria-label^="Easy Apply"]',
        'a[aria-label*="Easy Apply"]',
        'button[aria-label*="Easy Apply"]',
        'button.jobs-apply-button',
        'button#jobs-apply-button-id',
    ]
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                if not await el.is_visible():
                    continue
                aria = await el.get_attribute("aria-label") or ""
                text = (await el.inner_text()).strip()

                # Skip "Easy Apply filter" search-page filter pills
                if "filter" in aria.lower():
                    continue

                if "Easy Apply" in aria or text == "Easy Apply":
                    return el, "easy_apply"
        except Exception:
            continue

    # Last fallback: scan all visible buttons/anchors
    try:
        for el in await page.query_selector_all("button:visible, a:visible"):
            try:
                aria = await el.get_attribute("aria-label") or ""
                text = (await el.inner_text()).strip()
                if "filter" in aria.lower():
                    continue
                if text == "Easy Apply" or aria.startswith("Easy Apply"):
                    return el, "easy_apply"
            except Exception:
                continue
    except Exception:
        pass

    return None, None


async def search_easy_apply_jobs(page, query: str, location: str = "Berlin") -> list[dict]:
    """Search LinkedIn jobs filtered to Easy Apply only."""
    import urllib.parse
    q = urllib.parse.quote(query)
    loc = urllib.parse.quote(location)
    url = (
        f"https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}"
        f"&f_AL=true&f_TPR=r604800"  # Easy Apply only, past week
    )

    logger.info(f"Searching: '{query}' in {location}")
    # LinkedIn occasionally hangs hard on /jobs/search/. Retry once before giving up
    # so a single slow load doesn't tank the whole run.
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

    # Scroll
    for _ in range(4):
        await page.keyboard.press("End")
        await asyncio.sleep(_human_delay(2, 3))

    jobs = []
    cards = await page.query_selector_all(
        "li.jobs-search-results__list-item, .job-card-container, [data-occludable-job-id]"
    )
    logger.info(f"  Found {len(cards)} job cards")

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

            loc_el = await card.query_selector('.job-card-container__metadata-item, [class*="caption-wrapper"]')
            loc_text = (await loc_el.inner_text()).strip() if loc_el else location

            # Job ID
            job_id = await card.get_attribute("data-occludable-job-id")
            if not job_id:
                link_el = await card.query_selector("a[href*='/jobs/view/']")
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        job_id = href.split("/jobs/view/")[-1].split("/")[0].split("?")[0]

            if not job_id or not title:
                continue

            jobs.append({
                "title": title,
                "company": company,
                "location": loc_text,
                "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                "source": "linkedin",
                "external_id": f"li_{job_id}",
                "job_id_li": job_id,
            })
        except Exception:
            continue

    return jobs


async def get_hiring_manager_info(page, job: dict) -> dict | None:
    """Try to find the hiring manager / poster on the job page."""
    try:
        # LinkedIn 2026: hiring manager link is in "Meet the hiring team" section
        # or "Posted by" line at top of job description
        candidates = await page.query_selector_all(
            'a[href*="/in/"][data-test-app-aware-link], '
            'a[href*="/in/"][class*="hirer"], '
            '.hirer-card a[href*="/in/"], '
            '.jobs-poster__name a, '
            'a[data-test-id*="hirer"]'
        )

        for el in candidates:
            try:
                if not await el.is_visible():
                    continue
                href = await el.get_attribute("href") or ""
                if "/in/" not in href:
                    continue
                # Reject company links (only profile links)
                if "/company/" in href or "/school/" in href:
                    continue

                name = (await el.inner_text()).strip().split("\n")[0]
                if not name:
                    continue

                # Clean URL (remove query params)
                clean_url = href.split("?")[0]
                if not clean_url.startswith("http"):
                    clean_url = "https://www.linkedin.com" + clean_url

                return {
                    "name": name,
                    "profile_url": clean_url,
                }
            except Exception:
                continue
    except Exception:
        pass
    return None


async def send_connection_request(page, profile_url: str, name: str, role: str) -> bool:
    """Open profile and send connection request with note."""
    try:
        await page.goto(profile_url, timeout=20000)
        await asyncio.sleep(_human_delay(4, 6))

        # Click Connect button (might be in More menu)
        connect_btn = await page.query_selector('button[aria-label*="Connect"], button:has-text("Connect")')

        if not connect_btn or not await connect_btn.is_visible():
            # Look in More dropdown
            more_btn = await page.query_selector('button[aria-label*="More actions"], button:has-text("More")')
            if more_btn:
                await more_btn.click()
                await asyncio.sleep(_human_delay(1, 2))
                connect_btn = await page.query_selector('div[aria-label*="Connect"], div[role="menuitem"]:has-text("Connect")')

        if not connect_btn:
            logger.info("    No Connect button (already connected or 3rd degree)")
            return False

        await connect_btn.click()
        await asyncio.sleep(_human_delay(2, 3))

        # Click "Add a note"
        add_note = await page.query_selector('button[aria-label*="Add a note"]')
        if add_note:
            await add_note.click()
            await asyncio.sleep(_human_delay(1, 2))

            # Compose role-specific fit-focused note (<300 chars)
            note = generate_connect_note(name, role)

            note_field = await page.query_selector('textarea[name="message"], textarea#custom-message')
            if note_field:
                await note_field.fill(note)
                await asyncio.sleep(_human_delay(1, 2))

        # Send invite
        send_btn = await page.query_selector('button[aria-label*="Send invitation"], button[aria-label="Send"], button:has-text("Send")')
        if send_btn:
            await send_btn.click()
            await asyncio.sleep(_human_delay(2, 3))
            return True
    except Exception as e:
        logger.warning(f"    Connection request failed: {str(e)[:80]}")

    return False


async def fill_fields_with_classifier(page, cover_text: str, dry_run: bool = False) -> list[tuple]:
    """Fill all visible empty fields using the form classifier.

    Per CLAUDE.md CRITICAL rule: classify EVERY field by its label before filling.
    Unmatched fields are SKIPPED — never guessed, never default-Yes, never blind
    cover-letter dumps. Returns a log of (kind, label, value, action) for preview.
    """
    actions: list[tuple] = []

    # ---- Resume upload (only when the file input is present) ----
    file_input = await page.query_selector('input[type="file"]')
    if file_input and RESUME_FOR_RUN.exists():
        if not dry_run:
            try:
                await file_input.set_input_files(str(RESUME_FOR_RUN))
                await asyncio.sleep(_human_delay(2, 3))
            except Exception:
                pass
        actions.append(("file", "resume upload", RESUME_FOR_RUN.name, "filled"))

    # ---- Free-text / number / email / tel / url inputs ----
    for inp in await page.query_selector_all(
        'input[type="text"]:visible, input[type="tel"]:visible, '
        'input[type="email"]:visible, input[type="number"]:visible, input[type="url"]:visible'
    ):
        try:
            if (await inp.input_value() or "").strip():
                continue
            label = await get_field_label(page, inp)
            qa = classify_field(label)
            if not qa:
                actions.append(("text", label[:60], "", "SKIP — no match"))
                continue
            ftype = qa.get("field_type")
            if ftype in ("yes_no", "single_select", "long_text", "file_upload", "confirm_only"):
                actions.append(("text", label[:60], "", f"SKIP — handled as {ftype}"))
                continue
            val = qa.get("answer", "")
            if not val:
                actions.append(("text", label[:60], "", "SKIP — no answer value"))
                continue
            if not dry_run:
                await inp.fill(str(val))
            actions.append(("text", label[:60], str(val), "filled"))
        except Exception:
            continue

    # ---- Textareas: ONLY fill when classified as a cover-letter/long-text field ----
    for ta in await page.query_selector_all("textarea:visible"):
        try:
            if (await ta.input_value() or "").strip():
                continue
            label = await get_field_label(page, ta)
            qa = classify_field(label)
            if not qa or qa.get("field_type") != "long_text":
                # The exact bug we must NOT repeat: do NOT dump cover letter here.
                actions.append(("textarea", label[:60], "", "SKIP — not a cover-letter field"))
                continue
            val = cover_text if qa.get("answer_type") == "cover_letter" else qa.get("answer", "")
            if not val:
                actions.append(("textarea", label[:60], "", "SKIP — no answer value"))
                continue
            if not dry_run:
                await ta.fill(str(val)[:1800])
            actions.append(("textarea", label[:60], (str(val)[:40] + "…"), "filled"))
        except Exception:
            continue

    # ---- Radio groups (yes/no & single select): match the desired answer option ----
    for fs in await page.query_selector_all('fieldset[role="radiogroup"]:visible'):
        try:
            legend = await fs.query_selector("legend")
            legend_text = (await legend.inner_text()).strip() if legend else ""
            qa = classify_field(legend_text)
            if not qa or "answer" not in qa:
                actions.append(("radio", legend_text[:60], "", "SKIP — no match (not guessing)"))
                continue
            want = str(qa["answer"]).strip().lower()
            target = None
            for opt in await fs.query_selector_all('input[type="radio"]'):
                olabel = (await get_field_label(page, opt)).strip().lower()
                oval = (await opt.get_attribute("value") or "").strip().lower()
                if want == olabel or want == oval or olabel.startswith(want):
                    target = opt
                    break
            if not target:
                actions.append(("radio", legend_text[:60], qa["answer"], "SKIP — option not found"))
                continue
            if not dry_run and not await target.is_checked():
                await target.click(force=True)
                await asyncio.sleep(_human_delay(0.3, 0.7))
            actions.append(("radio", legend_text[:60], qa["answer"], "selected"))
        except Exception:
            continue

    # ---- Selects ----
    for sel in await page.query_selector_all("select:visible"):
        try:
            current = await sel.input_value()
            if current and current not in ("Select an option", ""):
                continue
            label = await get_field_label(page, sel)
            qa = classify_field(label)
            if not qa or "answer" not in qa:
                actions.append(("select", label[:60], "", "SKIP — no match"))
                continue
            want = str(qa["answer"]).strip().lower()
            chosen = None
            for opt in await sel.query_selector_all("option"):
                otext = (await opt.inner_text()).strip().lower()
                if want and want in otext:
                    chosen = await opt.get_attribute("value")
                    break
            if chosen is None:
                actions.append(("select", label[:60], qa["answer"], "SKIP — option not found"))
                continue
            if not dry_run:
                await sel.select_option(value=chosen)
                await asyncio.sleep(_human_delay(0.3, 0.7))
            actions.append(("select", label[:60], qa["answer"], "selected"))
        except Exception:
            continue

    return actions


async def apply_easy_apply(page, job: dict, cover: str) -> dict:
    """Submit a LinkedIn Easy Apply application."""
    outcome = {"status": "failed", "notes": "", "hiring_manager": None}

    try:
        await page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
        # Wait for the actual apply button to appear (LinkedIn loads via JS)
        try:
            await page.wait_for_selector(
                'button.jobs-apply-button, button#jobs-apply-button-id',
                timeout=15000,
                state="visible",
            )
        except Exception:
            pass
        await asyncio.sleep(_human_delay(2, 4))

        # Look for hiring manager BEFORE applying (job page still showing details)
        hm = await get_hiring_manager_info(page, job)
        outcome["hiring_manager"] = hm

        # Find Easy Apply button
        btn, btn_type = await find_easy_apply_button(page)
        if not btn:
            outcome["notes"] = "no Easy Apply button found"
            return outcome

        btn_text = (await btn.inner_text()).strip()
        aria = await btn.get_attribute("aria-label") or ""

        if "Applied" in btn_text or "Applied" in aria:
            outcome["status"] = "already_applied"
            return outcome

        # External apply (e.g., "Apply on company website")
        if btn_type == "apply" and "Easy Apply" not in btn_text and "Easy Apply" not in aria:
            outcome["notes"] = f"external apply (button: '{btn_text}')"
            return outcome

        await btn.click()
        await asyncio.sleep(_human_delay(3, 5))

        # Multi-step modal — some companies add 10+ screening questions
        max_steps = 14
        for step in range(max_steps):
            # Classify-then-fill EVERY field. Unmatched fields are skipped, never
            # guessed (CLAUDE.md CRITICAL rule). Returns a per-field action log.
            field_actions = await fill_fields_with_classifier(page, cover, dry_run=False)
            skipped = [a for a in field_actions if str(a[3]).startswith("SKIP")]
            if skipped:
                for a in skipped:
                    logger.info(f"      · {a[0]} '{a[1]}' → {a[3]}")

            # Action buttons
            submit_btn = await page.query_selector('button[aria-label="Submit application"]')
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(_human_delay(3, 5))
                outcome["status"] = "submitted"
                outcome["notes"] = f"submitted on step {step+1}"
                return outcome

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

            outcome["notes"] = f"stuck on step {step+1}"
            try:
                await take_screenshot(page, str(SCREENSHOTS / f"stuck_{job['external_id']}_step{step+1}.png"))
            except Exception:
                pass
            return outcome

        outcome["notes"] = "exceeded 8 steps"

    except Exception as e:
        outcome["notes"] = f"error: {str(e)[:120]}"

    return outcome


async def main():
    await db.init_db()

    print("=" * 70)
    print("LINKEDIN EASY APPLY + HIRING MANAGER CONNECTIONS")
    print("=" * 70)

    # Scope (user choice 2026-06-13): Dubai + Berlin + Japan with visa sponsorship/relocation.
    queries_locations = [
        # === DUBAI / UAE (visa sponsorship common) ===
        ("AI engineer", "Dubai"),
        ("machine learning engineer", "Dubai"),
        ("generative AI engineer", "Dubai"),
        ("visa sponsorship AI engineer", "Dubai"),
        ("relocation AI engineer", "Dubai"),
        ("LLM engineer", "United Arab Emirates"),
        # === BERLIN / GERMANY (EU Blue Card path) ===
        ("AI engineer", "Berlin"),
        ("machine learning engineer", "Berlin"),
        ("senior AI engineer", "Berlin"),
        ("visa sponsorship AI engineer", "Berlin"),
        ("blue card AI engineer", "Germany"),
        ("relocation machine learning engineer", "Germany"),
        # === JAPAN (high visa sponsorship for tech) ===
        ("AI engineer", "Tokyo"),
        ("machine learning engineer", "Tokyo"),
        ("visa sponsorship AI engineer", "Japan"),
        ("relocation AI engineer", "Japan"),
        ("LLM engineer", "Japan"),
        # === REMOTE / FREELANCE / CONTRACT (worldwide) ===
        ("freelance AI engineer", "Worldwide"),
        ("freelance LLM engineer", "Worldwide"),
        ("contract AI engineer", "Worldwide"),
        ("remote machine learning engineer", "Worldwide"),
    ]

    connections_sent_today = 0

    async with get_browser(headless=False) as page:
        ok = await linkedin_login(page)
        if not ok:
            logger.error("LinkedIn login failed")
            return
        logger.info("LinkedIn session active")

        all_jobs = []
        for q, loc in queries_locations:
            jobs = await search_easy_apply_jobs(page, q, loc)
            all_jobs.extend(jobs)

        seen = set()
        unique = []
        for j in all_jobs:
            if j["external_id"] in seen:
                continue
            seen.add(j["external_id"])
            unique.append(j)

        logger.info(f"Total unique jobs: {len(unique)}")

        # Use all jobs we haven't already applied to; insert new ones
        candidates = []
        for j in unique:
            existing_id = None
            conn = await db.get_db()
            cursor = await conn.execute(
                "SELECT id, fit_score, status FROM searched_jobs WHERE external_id = ?",
                (j["external_id"],),
            )
            row = await cursor.fetchone()
            await conn.close()

            if row:
                if row["status"] == "applied":
                    continue  # already applied — skip
                existing_id = row["id"]
                j["id"] = existing_id
                j["fit_score"] = row["fit_score"]
            else:
                # Use keyword-only scorer to skip LLM latency (51 jobs × 20s = 17min wasted)
                j["fit_score"] = ai_engine._keyword_score(j["title"], j["company"], "")
                j["id"] = await db.insert_job(j)
            candidates.append(j)

        candidates.sort(key=lambda x: x.get("fit_score", 0), reverse=True)

        # HARD FILTER: outside-India + remote + freelance only (per user instruction 2026-05-16)
        # See memory/sachin_target_filter.md — apply BEFORE the daily cap so the cap
        # counts only eligible jobs.
        INDIA_CITIES = {
            "india", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi",
            "hyderabad", "pune", "chennai", "kolkata", "gurgaon", "gurugram",
            "noida", "ncr", "ahmedabad", "jaipur", "kochi", "trivandrum",
            "thiruvananthapuram", "indore", "lucknow", "chandigarh",
        }

        def _is_india_only(loc_text: str) -> bool:
            if not loc_text:
                return False
            lt = loc_text.lower()
            # "Remote, India" or just "India" → skip; "Remote (Worldwide)" → keep
            return any(c in lt for c in INDIA_CITIES)

        eligible = []
        skipped_india = 0
        for j in candidates:
            if _is_india_only(j.get("location", "")):
                skipped_india += 1
                continue
            eligible.append(j)
        if skipped_india:
            print(f"  ⏭  Skipped {skipped_india} India-based jobs (Sachin target filter)")

        # Post-throttle safe cap (see memory/linkedin_throttle_2026_05_19.md)
        to_apply = eligible[:MAX_APPLY]

        print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Top {len(to_apply)} eligible jobs"
              f" (of {len(eligible)} eligible, cap={MAX_APPLY}):")
        for j in to_apply:
            print(f"  [{j['fit_score']:3d}] {j['title'][:48]:48s} @ {j['company'][:24]:24s} | {j.get('location','')[:24]}")

        if DRY_RUN:
            print("\n" + "=" * 70)
            print("CLASSIFIER SELF-TEST — how each screening question would be answered")
            print("(proves cover-letter + work-authorization are handled correctly)")
            print("=" * 70)
            probe_questions = [
                "How many years of work experience do you have?",
                "Will you now or in the future require visa sponsorship?",
                "Are you legally authorized to work without sponsorship?",
                "Are you a citizen of the United Arab Emirates?",
                "What is your expected annual salary?",
                "What is your current notice period?",
                "Are you willing to relocate to Dubai?",
                "Are you comfortable working onsite?",
                "Phone number",
                "What city do you currently live in?",
                "LinkedIn profile URL",
                "Cover letter",
                "Why are you interested in this role?",
                "Tell us about yourself",
                "Have you ever been convicted of a felony?",
                "Gender",
                "Random unmapped question about your favourite colour",  # must SKIP
            ]
            for q in probe_questions:
                qa = classify_field(q)
                if not qa:
                    print(f"  SKIP   | {q[:52]:52s} | (no match — field left blank)")
                    continue
                if qa.get("answer_type") == "cover_letter":
                    ans = "<AI-generated cover letter>"
                elif qa.get("answer_type") == "resume":
                    ans = f"<upload {RESUME_FOR_RUN.name}>"
                else:
                    ans = str(qa.get("answer", "")).replace("\n", " ")[:48]
                print(f"  {qa.get('field_type','?')[:8]:8s} | {q[:52]:52s} | {ans}")
            print("\n" + "=" * 70)
            print(f"DRY RUN complete — NOTHING submitted. Attachment on apply: {RESUME_FOR_RUN.name}")
            print("Re-run with --live  (optionally --max=N) to submit real applications.")
            print("=" * 70)
            return

        outcomes = []
        for i, job in enumerate(to_apply, 1):
            print(f"\n[{i}/{len(to_apply)}] {job['title']} @ {job['company']}  ({job.get('location','')})")
            cover = ai_engine.generate_cover_letter(job)
            result = await apply_easy_apply(page, job, cover)
            outcomes.append({**job, **result})

            if result["status"] == "submitted":
                await db.mark_applied(job["id"], cover, "")
                print(f"  ✓ APPLICATION SUBMITTED")

                # Try to connect to hiring manager (under daily limit)
                hm = result.get("hiring_manager")
                if hm and connections_sent_today < DAILY_CONNECTION_LIMIT:
                    print(f"  → Connecting to {hm['name']}...")
                    sent = await send_connection_request(page, hm["profile_url"], hm["name"], job["title"])
                    if sent:
                        connections_sent_today += 1
                        print(f"    ✓ Connection request sent ({connections_sent_today}/{DAILY_CONNECTION_LIMIT})")
                    else:
                        print(f"    ~ could not send connection")
            elif result["status"] == "already_applied":
                print(f"  ~ already applied")
            else:
                print(f"  ✗ {result['notes']}")

            # POST-WARNING (2026-05-19): much longer human-like delays + cooldown every 5 apps
            await asyncio.sleep(_human_delay(30, 55))
            if (i + 1) % 5 == 0 and (i + 1) < len(to_apply):
                cooldown = _human_delay(120, 180)
                print(f"  ⏸ Long cooldown {cooldown:.0f}s (post-warning safety)")
                await asyncio.sleep(cooldown)

        # Summary
        submitted = [o for o in outcomes if o["status"] == "submitted"]
        print(f"\n{'='*70}")
        print(f"FINAL: {len(submitted)} REAL applications submitted")
        print(f"       {connections_sent_today} connection requests sent to hiring managers")
        print(f"{'='*70}")
        for o in submitted:
            hm_str = f" + connect→{o['hiring_manager']['name']}" if o.get("hiring_manager") else ""
            print(f"  ✓ {o['title']} @ {o['company']}{hm_str}")


if __name__ == "__main__":
    asyncio.run(main())
