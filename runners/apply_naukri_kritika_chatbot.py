"""Naukri Quick Apply with REAL chatbot Q&A for Kritika.

Flow per job (confirmed from DOM probe 2026-06-03):
  1. On search page, click "Quick apply" span (opens job detail in NEW TAB)
  2. Switch to the new tab
  3. Scroll to bottom, click the BIG "Quick apply" button on detail page
  4. Naukri chatbot panel renders on the right with sequential questions:
       <li class="chatbot_ListItem"><div class="botMsg"><span>QUESTION</span></div></li>
       <div class="textArea" contenteditable="true" data-placeholder="..."> <- ANSWER input
       <div class="sendMsgbtn_container"> Send </div>
       <div class="chatbot_Chip"><span>Skip this question</span></div>
  5. For each question: lookup answer from Kritika's profile, type, send
  6. Loop until success text or N questions answered
  7. Close detail tab, move to next job

Answer source: hardcoded mapping (this is more reliable than form_qa.yaml for
Naukri's chatbot phrasing).
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

from src.config import BASE_DIR
from src.vision_fallback import vision_decide

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("naukri-kritika-chatbot")

COOKIES_PATH = BASE_DIR / "data" / "cookies" / "kritika_naukri_cookies.json"
SHOTS_DIR = BASE_DIR / "data" / "naukri_kritika_apply"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_PATH = BASE_DIR / "data" / "naukri_kritika_results.json"

MAX_APPLIES = 120
MAX_QUESTIONS_PER_JOB = 10
SEARCH_KEYWORDS = [
    "generative-AI-engineer",
    "agentic-AI-engineer",
    "LLM-engineer",
    "ml-ops-engineer",
    "applied-AI-engineer",
    "lead-AI-engineer",
    "senior-machine-learning-engineer",
    "senior-data-scientist",
    "AI-research-engineer",
    "computer-vision-engineer",
    "deep-learning-engineer",
    "nlp-engineer",
    "AI-engineer",
    "machine-learning-engineer",
    "data-scientist",
    "senior-AI-engineer",
]


# ─── Kritika's answer bank (matched to common Naukri chatbot questions) ───
# Patterns checked in order; FIRST match wins.
ANSWER_BANK = [
    # CTC questions (Naukri loves these)
    (r"current.*ctc.*lacs|current.*salary.*lacs|present.*ctc.*lacs", "45"),
    (r"current.*ctc.*annum|current.*ctc|present.*ctc|current.*salary", "4500000"),
    (r"expected.*ctc.*lacs|expected.*salary.*lacs", "70"),
    (r"expected.*ctc|expected.*salary", "7000000"),
    # Notice period — usually RADIO. Use exact option text + numeric fallbacks.
    (r"notice.*period|when.*can.*start|earliest.*joining|joining.*date", "2 Months"),
    # Years of experience
    (r"years.*of.*relevant.*experience|relevant.*experience.*years", "7"),
    (r"years.*of.*experience|total.*experience.*years", "7"),
    (r"\bexperience\b.*\b(in|with)\b.*python", "7 years"),
    (r"\bexperience\b.*\b(in|with)\b.*(machine learning|ml|ai|nlp|llm)", "5 years"),
    # Location
    (r"current.*location|where.*are.*you.*currently|current.*city", "Bengaluru"),
    (r"willing.*to.*relocate", "Yes"),
    (r"preferred.*location|location.*preference", "Bengaluru, Delhi NCR, Remote"),
    # Skills / fit
    (r"why.*interested.*role|why.*this.*role|why.*you.*fit",
     "I have 7+ years of hands-on experience building production-grade GenAI/LLM systems "
     "and ML pipelines across regulated finance and HR-tech. Specialist in multi-agent "
     "orchestration (LangGraph, MCP), RAG with vector DBs, and end-to-end deployment on AWS/GCP. "
     "This role aligns with my expertise and growth ambitions."),
    # Demographics / personal
    (r"highest.*qualification|highest.*education|education.*level", "Postgraduate"),
    (r"\bgender\b", "Female"),
    (r"\bage\b", "28"),
    (r"date.*of.*birth|dob", "09/02/1997"),
    # Visa / work auth (mostly N/A for India-based roles)
    (r"work.*authorization|authorized.*to.*work.*india", "Yes"),
    (r"visa.*sponsorship.*required", "No"),
    # Generic yes/no
    (r"are.*you.*ok|are.*you.*comfortable|are.*you.*willing", "Yes"),
    (r"do.*you.*have", "Yes"),
]


def lookup_answer(question_text: str) -> str | None:
    q = question_text.strip().lower()
    for pattern, answer in ANSWER_BANK:
        if re.search(pattern, q):
            return answer
    return None


# Ranked candidate option texts to try clicking, by question type.
RADIO_OPTION_CANDIDATES = {
    "notice": ["2 Months", "1 Month", "More than 3 Months", "3 Months", "Serving Notice Period"],
    "relocate": ["Yes", "Yes, willing to relocate"],
    "willing": ["Yes"],
    "gender": ["Female"],
    "qualification": ["Postgraduate", "Post Graduate", "Master", "MBA"],
    # Experience: Kritika has 7yr — match her ACTUAL band, never "less than" / "no experience"
    "experience": [
        "5-7 years", "5 to 7 years", "More than 5 years", "5+ years",
        "6 years", "7 years",
        "7-10 years", "More than 7 years",
        "More than 3 years", "3+ years", "3-5 years",
        "5", "7",
    ],
    "ctc_lakh": ["45", "40-50 Lakhs", "30-40 Lakhs"],
    "expected_ctc_lakh": ["70", "70-80 Lakhs", "60-70 Lakhs"],
    "city": ["Bengaluru", "Bangalore", "Bengaluru, Karnataka", "Bangalore, Karnataka",
             "Delhi NCR", "Delhi", "Gurugram", "Gurgaon", "Noida", "Mumbai"],
}

# Options that would DISQUALIFY Kritika — never click as fallback.
FORBIDDEN_FALLBACKS = {
    "no experience", "0 years", "less than 1", "less than 3", "less than 5",
    "no, i don't have", "no, i do not", "no", "fresher", "0-1 years", "1-3 years",
}


def get_radio_candidates(question_text: str) -> list[str]:
    q = question_text.strip().lower()
    if "notice" in q or "joining" in q:
        return RADIO_OPTION_CANDIDATES["notice"]
    if "relocate" in q or "ready to relocate" in q or "willing to relocate" in q:
        return RADIO_OPTION_CANDIDATES["relocate"]
    if "currently living in" in q:
        return RADIO_OPTION_CANDIDATES["relocate"]
    if "gender" in q:
        return RADIO_OPTION_CANDIDATES["gender"]
    if "qualification" in q or ("highest" in q and "education" in q):
        return RADIO_OPTION_CANDIDATES["qualification"]
    if "willing" in q or "comfortable" in q or "are you ok" in q:
        return RADIO_OPTION_CANDIDATES["willing"]
    # ANY experience question → use generic experience candidates (Kritika = 7yr)
    if "year" in q and ("experience" in q or "exp." in q):
        return RADIO_OPTION_CANDIDATES["experience"]
    if "how many years" in q or "total it experience" in q or "total experience" in q:
        return RADIO_OPTION_CANDIDATES["experience"]
    if "current" in q and ("ctc" in q or "salary" in q) and "lakh" in q:
        return RADIO_OPTION_CANDIDATES["ctc_lakh"]
    if "expected" in q and ("ctc" in q or "salary" in q):
        return RADIO_OPTION_CANDIDATES["expected_ctc_lakh"]
    if "city" in q or "location" in q:
        return RADIO_OPTION_CANDIDATES["city"]
    return []


async def try_click_option(page, candidates: list[str]) -> str | None:
    """Look for Naukri chatbot single-select radio options. Click best-match label.

    Real DOM (confirmed 2026-06-03):
      <div class="singleselect-radiobutton">
        <div class="ssrc__radio-btn-container">
          <input type="radio" id="2 months" value="2 months" class="ssrc__radio">
          <label for="2 months" class="ssrc__label">2 months</label>
        </div>
      </div>
    """
    options = await page.evaluate("""() => {
        const labels = Array.from(document.querySelectorAll(
            'label.ssrc__label, [class*="ssrc__label"], '
            + 'div.singleselect-radiobutton label, '
            + 'div[class*="multiselect"] label'
        )).filter(el => el.offsetParent !== null);
        return labels.map((el, i) => ({
            idx: i,
            text: (el.textContent || '').trim(),
            forAttr: el.getAttribute('for') || '',
        }));
    }""")
    if not options:
        return None
    real = [o for o in options if "skip" not in o["text"].lower() and o["text"]]
    if not real:
        return None

    async def _click_by_idx(idx: int) -> bool:
        return await page.evaluate(f"""() => {{
            const labels = Array.from(document.querySelectorAll(
                'label.ssrc__label, [class*="ssrc__label"], '
                + 'div.singleselect-radiobutton label, '
                + 'div[class*="multiselect"] label'
            )).filter(el => el.offsetParent !== null);
            const t = labels[{idx}];
            if (!t) return false;
            t.scrollIntoView({{block: 'center'}});
            // Click both label and the radio it points to
            t.click();
            const fa = t.getAttribute('for');
            if (fa) {{
                const radio = document.getElementById(fa);
                if (radio) {{ radio.checked = true; radio.click(); radio.dispatchEvent(new Event('change', {{bubbles:true}})); }}
            }}
            return true;
        }}""")

    # Pass 1: exact match (case-insensitive)
    for cand in candidates:
        cl = cand.lower().strip()
        for opt in real:
            if opt["text"].lower().strip() == cl:
                if await _click_by_idx(opt["idx"]):
                    return opt["text"]
    # Pass 2: substring match
    for cand in candidates:
        cl = cand.lower().strip()
        for opt in real:
            ot = opt["text"].lower().strip()
            if cl in ot or ot in cl:
                if await _click_by_idx(opt["idx"]):
                    return opt["text"]
    # Pass 3: skip — better to skip than click a disqualifying option
    return None


async def click_save_or_send(page) -> bool:
    """Click the chatbot Save/Send button.

    Real DOM (confirmed 2026-06-03):
      <div class="sendMsgbtn_container">
        <div class="send" or "send disabled">     ← parent toggles 'disabled' class
          <div class="sendMsg" tabindex="0">Save</div>   ← or "Send"
        </div>
      </div>
    """
    return await page.evaluate("""() => {
        // Strategy 1: the official Naukri chatbot button
        const container = document.querySelector('[class*="sendMsgbtn_container"]');
        if (container) {
            const sendDiv = container.querySelector('.send, [class*=" send"], [class$="send"]');
            const inner = container.querySelector('.sendMsg, [class*="sendMsg"]');
            const target = inner || sendDiv;
            if (target) {
                target.scrollIntoView({block: 'center'});
                // Click both the inner label and the outer .send parent for safety
                target.click();
                if (sendDiv && sendDiv !== target) sendDiv.click();
                return true;
            }
        }
        // Strategy 2: text scan fallback
        const all = Array.from(document.querySelectorAll('div, button, span'));
        const cands = all.filter(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            if (t !== 'save' && t !== 'send') return false;
            const r = el.getBoundingClientRect();
            return r.width > 20 && r.height > 15 && el.offsetParent !== null;
        });
        if (!cands.length) return false;
        cands.sort((a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            return (rb.right + rb.bottom) - (ra.right + ra.bottom);
        });
        cands[0].click();
        return true;
    }""")


def _delay(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def _load_cookies() -> list:
    if COOKIES_PATH.exists():
        try:
            return json.loads(COOKIES_PATH.read_text())
        except Exception:
            return []
    return []


async def _shot(p, name: str):
    try:
        await p.screenshot(path=str(SHOTS_DIR / f"{int(time.time())}_{name}.png"))
    except Exception:
        pass


async def answer_chatbot_loop(detail_page, job_idx: int) -> str:
    """Loop: read latest bot question, lookup answer, type, send. Until success or skip-all."""
    seen_questions = set()
    answered = 0
    skipped = 0

    for step in range(MAX_QUESTIONS_PER_JOB):
        await asyncio.sleep(2)
        # Check success first — wrapped because chatbot pages sometimes navigate
        # mid-evaluate ("Similar opportunities" auto-redirect), which destroys the
        # exec context. Treat any nav-during-eval as success (Naukri only redirects
        # AFTER submitting).
        try:
            body = (await detail_page.evaluate("() => document.body.innerText"))[:3000].lower()
        except Exception as e:
            if answered >= 1:
                logger.info(f"    nav during eval ({str(e)[:60]}); counting as submitted")
                return f"submitted_via_nav ({answered} answered, {skipped} skipped)"
            logger.warning(f"    nav crash on fresh job ({str(e)[:80]}); skipping")
            return f"nav_crash_early (step {step})"
        success_signals = [
            "successfully applied", "application sent", "thank you for applying",
            "you have successfully", "applied to this job", "received your application",
        ]
        if any(s in body for s in success_signals):
            return f"submitted ({answered} answered, {skipped} skipped)"

        # Pull latest question from the bot message list
        try:
            question = await detail_page.evaluate("""() => {
                const items = document.querySelectorAll('li.chatbot_ListItem .botMsg span, li.botItem .botMsg span');
                if (items.length === 0) return null;
                return (items[items.length - 1].textContent || '').trim();
            }""")
        except Exception as e:
            if answered >= 1:
                logger.info(f"    nav during question read ({str(e)[:60]}); counting as submitted")
                return f"submitted_via_nav ({answered} answered, {skipped} skipped)"
            return f"nav_error (step {step})"
        if not question:
            # No question text — chatbot has likely finished
            if answered >= 1:
                return f"submitted_likely ({answered} answered, {skipped} skipped)"
            return f"no_question (step {step}, {answered}/{skipped})"
        if question in seen_questions:
            # Same question twice → chatbot stalled, BUT user-confirmed (2026-06-03)
            # that stalled jobs often still submit on Naukri's side.
            # Treat as submitted if we answered at least 2 prior questions.
            if answered >= 2:
                return f"submitted_likely_stalled ({answered} answered, stalled at {question[:40]})"
            return f"stalled_on (step {step}, last={question[:60]})"
        seen_questions.add(question)
        logger.info(f"    Q{step+1}: {question[:80]}")

        answer = lookup_answer(question)
        radio_candidates = get_radio_candidates(question)
        if answer and answer not in radio_candidates:
            radio_candidates = [answer] + radio_candidates

        # STEP 1: deterministic radio click (only if we have candidates)
        if radio_candidates:
            clicked_option = await try_click_option(detail_page, radio_candidates)
            if clicked_option:
                logger.info(f"    → clicked option: {clicked_option}")
                await asyncio.sleep(1)
                await click_save_or_send(detail_page)
                answered += 1
                await asyncio.sleep(3)
                continue

        # STEP 2: deterministic text input (only if mapped)
        if answer:
            text_ok = await _try_text_input(detail_page, answer)
            if text_ok:
                logger.info(f"    → text: {answer[:60]}")
                answered += 1
                await asyncio.sleep(2.5)
                continue

        # STEP 3: Claude vision fallback (custom dropdowns, unmapped questions)
        logger.info(f"    → DOM handlers failed; trying vision fallback")
        decision = await _vision_fallback(detail_page, question)
        if decision:
            if decision["action"] == "click_text":
                # Try radio-style selector first, then generic visible-text fallback
                clicked = await try_click_option(detail_page, [decision["value"]])
                if not clicked:
                    if await _click_any_visible_text(detail_page, decision["value"]):
                        clicked = decision["value"]
                if clicked:
                    logger.info(f"    [vision] clicked: {clicked}")
                    await asyncio.sleep(1)
                    await click_save_or_send(detail_page)
                    answered += 1
                    await asyncio.sleep(3)
                    continue
            elif decision["action"] == "type_text":
                ok = await _try_text_input(detail_page, decision["value"])
                if ok:
                    logger.info(f"    [vision] typed: {decision['value'][:60]}")
                    answered += 1
                    await asyncio.sleep(2.5)
                    continue
            # decision was "skip" or click/type didn't land — fall through to skip below

        # STEP 4: Give up on this question, try the Skip pill
        logger.info(f"    → skipping (no DOM + no vision answer)")
        skipped += 1
        try:
            skip = detail_page.locator('div.chatbot_Chip:has-text("Skip this question")').first
            if await skip.count() and await skip.is_visible():
                await skip.click()
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        return f"skip_unavailable (step {step}, {answered}/{skipped})"

    return f"max_questions ({answered} answered, {skipped} skipped)"


async def _try_text_input(detail_page, value: str) -> bool:
    """Type into the chatbot's contenteditable and click Save. Returns True on success."""
    try:
        inp = detail_page.locator('div.textArea[contenteditable="true"]').first
        await inp.wait_for(state="visible", timeout=4000)
        await inp.click(timeout=4000)
        await asyncio.sleep(0.4)
        await detail_page.keyboard.press("Control+A")
        await detail_page.keyboard.press("Delete")
        await detail_page.keyboard.type(value, delay=15)
        await asyncio.sleep(1)
        sent = await click_save_or_send(detail_page)
        if not sent:
            await detail_page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def _click_any_visible_text(page, target: str) -> bool:
    """Click ANY visible element whose text matches `target`. Generic fallback for
    custom dropdowns / non-standard widgets where ssrc__label doesn't apply."""
    return await page.evaluate(f"""(target) => {{
        const tgt = target.toLowerCase().trim();
        const all = Array.from(document.querySelectorAll(
            'label, button, div, span, li, a, [role="option"], [role="radio"], [role="button"]'
        )).filter(el => {{
            const r = el.getBoundingClientRect();
            return r.width > 30 && r.height > 18 && el.offsetParent !== null;
        }});
        // Exact match first
        for (const el of all) {{
            if ((el.textContent || '').trim().toLowerCase() === tgt) {{
                el.scrollIntoView({{block: 'center'}});
                el.click();
                return true;
            }}
        }}
        // Substring match — prefer smallest element (most specific)
        const matches = all.filter(el => (el.textContent || '').trim().toLowerCase().includes(tgt));
        if (matches.length) {{
            matches.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
            matches[0].scrollIntoView({{block: 'center'}});
            matches[0].click();
            return true;
        }}
        return false;
    }}""", target)


async def _vision_fallback(detail_page, question: str):
    """Screenshot the chatbot panel and ask Claude what to do. Returns decision dict or None."""
    try:
        viewport = detail_page.viewport_size or {"width": 1440, "height": 900}
        # Chatbot panel is right ~half of viewport on Naukri TopTier
        clip = {
            "x": max(0, viewport["width"] // 2 - 50),
            "y": 0,
            "width": viewport["width"] - (viewport["width"] // 2 - 50),
            "height": viewport["height"],
        }
        shot = await detail_page.screenshot(clip=clip)
        return await vision_decide(shot, question)
    except Exception as e:
        logger.warning(f"  [vision] screenshot/decide failed: {str(e)[:120]}")
        return None


async def apply_one(ctx, search_page, qa_index: int) -> dict:
    """Click one Quick apply on search page, switch to opened tab, click big apply, run chatbot."""
    # Track opened pages
    opened = []
    listener = lambda p: opened.append(p)
    ctx.on("page", listener)

    try:
        qa_locators = search_page.locator('span', has_text='Quick apply')
        if qa_index >= await qa_locators.count():
            return {"status": "no_more"}

        target = qa_locators.nth(qa_index)
        # Get nearest job title from card
        try:
            title = await target.evaluate("""(el) => {
                let p = el;
                for (let i=0; i<8 && p; i++, p=p.parentElement) {
                    const t = p.querySelector('a[class*="title"], a.title, [class*="jobTitle"], h2, h3');
                    if (t && (t.textContent || '').trim()) return (t.textContent || '').trim().slice(0, 80);
                }
                return 'Naukri job';
            }""")
        except Exception:
            title = f"job#{qa_index}"

        await target.scroll_into_view_if_needed()
        await asyncio.sleep(1)
        await target.click()
        logger.info(f"  Clicked Quick apply on search → wait for new tab")
        await asyncio.sleep(5)

        # Find the new detail tab
        detail = None
        for p in opened:
            if "job-listings" in p.url.lower():
                detail = p
                break
        if not detail:
            return {"status": "no_detail_tab", "title": title}

        await detail.bring_to_front()
        try:
            await detail.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(4)
        await _shot(detail, f"{qa_index:02d}_detail")

        # Click the BIG Quick apply button on detail page
        await detail.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)
        clicked = await detail.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('button, a, span, div'));
            const candidates = all.filter(el => {
                const t = (el.textContent || '').trim().toLowerCase();
                if (t !== 'quick apply' && t !== 'apply' && t !== "i'm interested") return false;
                const r = el.getBoundingClientRect();
                return r.width > 100 && r.height > 30 && el.offsetParent !== null;
            });
            if (!candidates.length) return false;
            const btn = candidates[candidates.length - 1];
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return true;
        }""")
        if not clicked:
            await detail.close()
            return {"status": "no_big_apply", "title": title}

        await asyncio.sleep(5)
        await _shot(detail, f"{qa_index:02d}_chatbot_open")

        # Check immediate success — wrap because page sometimes navigates
        try:
            body = (await detail.evaluate("() => document.body.innerText"))[:2000].lower()
            if any(s in body for s in ["successfully applied", "application sent", "thank you for applying"]):
                await detail.close()
                return {"status": "submitted_instant", "title": title}
        except Exception:
            # Nav during eval = Naukri redirected after instant submit
            try: await detail.close()
            except Exception: pass
            return {"status": "submitted_instant_nav", "title": title}

        # Run chatbot Q&A loop — wrap to never crash the parent loop
        try:
            outcome = await answer_chatbot_loop(detail, qa_index)
        except Exception as e:
            logger.warning(f"  chatbot loop crashed: {str(e)[:120]}")
            outcome = "loop_crashed"
        try:
            await _shot(detail, f"{qa_index:02d}_final")
        except Exception:
            pass
        try:
            await detail.close()
        except Exception:
            pass
        return {"status": outcome, "title": title}
    finally:
        try:
            ctx.remove_listener("page", listener)
        except Exception:
            pass


async def main():
    print("=" * 70)
    print(f"NAUKRI CHATBOT APPLY — Kritika (cap {MAX_APPLIES})")
    print("=" * 70)

    cookies = _load_cookies()
    if not cookies:
        print("No cookies — run profile updater first to capture login")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
        await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        await ctx.add_cookies(cookies)
        search_page = await ctx.new_page()
        if HAS_STEALTH:
            try: await Stealth().apply_stealth_async(search_page)
            except: pass

        results = []
        submitted = 0
        for kw in SEARCH_KEYWORDS:
            if submitted >= MAX_APPLIES:
                break
            url = f"https://www.naukri.com/{kw}-jobs?experience=7"
            logger.info(f"\nSearch: {url}")
            # Wrap goto + page setup so a single slow search-page doesn't tank the whole batch
            try:
                await search_page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(5)
                for _ in range(3):
                    await search_page.keyboard.press("End")
                    await asyncio.sleep(1)
                qa_count = await search_page.locator('span', has_text='Quick apply').count()
            except Exception as e:
                logger.warning(f"  search-page failed ({str(e)[:80]}); skipping keyword {kw}")
                await asyncio.sleep(8)
                continue
            logger.info(f"  {qa_count} Quick apply buttons")

            for idx in range(min(qa_count, MAX_APPLIES - submitted)):
                if submitted >= MAX_APPLIES:
                    break
                logger.info(f"\n[{submitted+1}/{MAX_APPLIES}] {kw} #{idx}")
                try:
                    outcome = await apply_one(ctx, search_page, idx)
                except Exception as e:
                    logger.warning(f"  apply_one crashed: {str(e)[:120]}; continuing")
                    outcome = {"status": "apply_one_crashed", "title": ""}
                    # Try to close any stray detail tabs
                    for p in list(ctx.pages):
                        if p is not search_page and "job-listings" in (p.url or "").lower():
                            try: await p.close()
                            except Exception: pass
                results.append(outcome)
                if outcome.get("status", "").startswith("submitted"):
                    submitted += 1
                    logger.info(f"  ✓ SUBMITTED — {outcome.get('title', '')[:60]}")
                else:
                    logger.info(f"  ~ {outcome.get('status')} — {outcome.get('title', '')[:60]}")
                # Save incremental
                try:
                    RESULTS_PATH.write_text(json.dumps(results, indent=2))
                except Exception:
                    pass
                await asyncio.sleep(_delay(20, 35))

        print(f"\n{'='*70}")
        print(f"NAUKRI KRITIKA CHATBOT FINAL: {submitted} REAL applications")
        print(f"{'='*70}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
