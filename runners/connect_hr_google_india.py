"""Reach out to Google India recruiters/Talent Acquisition on LinkedIn.

Workflow:
  1. Login to Sachin's LinkedIn.
  2. Run LinkedIn People search for Google + Bangalore/Hyderabad recruiters
     working on ML / AI / Software hiring.
  3. For each surfaced recruiter:
       - Visit their profile, parse name + role/team
       - Build a personalized note using the v3_interest template
         (pre-apply outreach — Sachin hasn't applied yet, is asking for the
          recruiter's eyes before submitting through the Google careers portal)
       - Dedupe against `linkedin_notes_sent` (last 90 days)
       - If --live, send connection request; otherwise queue + preview

Defaults: cap=3, dry-run (`--live` required to actually send). Throttle: 90-180s
random gap between live sends. NEVER run in parallel with apply_linkedin.py for
the same account (LinkedIn rate-limits parallel sessions per IP).
"""

import asyncio
import json
import logging
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import aiosqlite

from src.browser import get_browser, linkedin_login, _human_delay
from src.config import BASE_DIR
from src.hr_connect import render_note

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("connect-hr-google-india")

DB_PATH = BASE_DIR / "data" / "jobs.db"
OUT_DIR = BASE_DIR / "data" / "hr_connect"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Google's LinkedIn company id (used in &currentCompany=<id> filter).
# Verified: 1441 = Google.
GOOGLE_COMPANY_ID = "1441"
# India geoUrn id on LinkedIn: 102713980 (India). For city-specific, Bangalore = 105214831,
# Hyderabad = 105556991. We use India geoUrn to widen — keyword filter narrows to recruiters.
INDIA_GEO_URN = "102713980"


def _arg_int(flag: str, default: int) -> int:
    for arg in sys.argv:
        if arg.startswith(flag + "="):
            try: return int(arg.split("=", 1)[1])
            except: pass
    return default


DRY_RUN = "--live" not in sys.argv
MAX_CONNECTS = _arg_int("--max", 3)
SCAN_PROFILES = _arg_int("--scan", 15)  # how many search-results profiles to inspect


# ─── DB helpers (shared schema with connect_hr_sachin) ────────────────────────

async def ensure_table():
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_notes_sent (
                id INTEGER PRIMARY KEY,
                profile_url TEXT UNIQUE NOT NULL,
                profile_name TEXT,
                company TEXT,
                role TEXT,
                variant TEXT,
                note_text TEXT,
                note_chars INTEGER,
                sent_at TEXT,
                status TEXT,
                dry_run INTEGER
            )
        """)
        await con.commit()


async def already_sent(profile_url: str, days: int = 90) -> bool:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT 1 FROM linkedin_notes_sent WHERE profile_url=? AND dry_run=0 AND sent_at>? LIMIT 1",
            (profile_url, cutoff),
        )
        return bool(await cur.fetchone())


async def record_note(profile_url: str, profile_name: str, company: str, role: str,
                     variant: str, note: str, status: str):
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute(
            "INSERT OR REPLACE INTO linkedin_notes_sent "
            "(profile_url, profile_name, company, role, variant, note_text, note_chars, sent_at, status, dry_run) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (profile_url, profile_name, company, role, variant, note, len(note),
             datetime.utcnow().isoformat(), status, 0 if not DRY_RUN else 1),
        )
        await con.commit()


# ─── LinkedIn People search ───────────────────────────────────────────────────

def build_search_urls() -> list[str]:
    """LinkedIn People search filtered to Google + India + recruiter keywords.
    Multiple keyword angles to find a diverse set."""
    base = "https://www.linkedin.com/search/results/people/"
    params_base = (
        f"?currentCompany=%5B%22{GOOGLE_COMPANY_ID}%22%5D"
        f"&geoUrn=%5B%22{INDIA_GEO_URN}%22%5D"
        f"&origin=FACETED_SEARCH"
    )
    keyword_sets = [
        "technical recruiter machine learning",
        "recruiter AI ML Bangalore",
        "recruiter software engineering Hyderabad",
        "talent acquisition AI ML",
    ]
    urls = []
    for kw in keyword_sets:
        q = urllib.parse.quote(kw)
        urls.append(f"{base}{params_base}&keywords={q}")
    return urls


async def harvest_search_results(page, search_url: str, limit: int = 10) -> list[dict]:
    """Visit a LinkedIn people-search URL, return profiles found (url, name, role_at_company)."""
    try:
        await page.goto(search_url, timeout=45000, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning(f"  goto failed: {str(e)[:120]}")
        return []
    await asyncio.sleep(_human_delay(4, 6))
    # Scroll a couple times to load more results
    for _ in range(3):
        await page.keyboard.press("End")
        await asyncio.sleep(_human_delay(1.5, 2.5))

    profiles = await page.evaluate("""(limit) => {
        // Search result cards: each has an anchor to /in/<slug>
        const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
        const seen = new Set();
        const out = [];
        for (const a of anchors) {
            const href = a.getAttribute('href').split('?')[0];
            if (!href.includes('/in/')) continue;
            if (seen.has(href)) continue;
            // Skip if anchor is a tiny avatar-only link (no text)
            const text = (a.textContent || '').replace(/\\s+/g,' ').trim();
            if (text.length < 3) continue;
            seen.add(href);
            const card = a.closest('li, div[class*="entity"]') || a.parentElement || a;
            const block = (card.textContent || '').replace(/\\s+/g,' ').trim();
            out.push({
                url: href.startsWith('http') ? href : 'https://www.linkedin.com' + href,
                anchorText: text.slice(0, 80),
                block: block.slice(0, 400),
            });
            if (out.length >= limit) return out;
        }
        return out;
    }""", limit)

    parsed = []
    for p in profiles:
        atext = p.get("anchorText", "")
        primary = re.split(r"[•·]|3rd|2nd|1st|View .{1,40} profile", atext, maxsplit=1)[0].strip()
        m = re.match(r"([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})", primary)
        name = m.group(1) if m else ""
        # Role hint: pull from block after name (typical pattern: Name · Title at Company · Location)
        block = p["block"]
        if name and name in block:
            after = block.split(name, 1)[1]
        else:
            after = block
        # Stop at common LinkedIn UI strings
        after = re.split(r"\b(Message|Connect|Follow|View .{1,40} profile|See more|Show all)\b|[•·]", after, maxsplit=1)[0]
        role_at_company = after.strip(" ·•,.")[:120]
        # Heuristic: must mention recruiter/talent OR explicitly Google to keep it on-target
        full_lower = block.lower()
        if not any(k in full_lower for k in ("recruiter", "talent acquisition", "talent partner",
                                              "sourcer", "hiring", "tech hr")):
            continue
        parsed.append({
            "url": p["url"],
            "name": name or "there",
            "role_at_company": role_at_company,
        })
    return parsed


# ─── Connect-send (same as connect_hr_sachin) ────────────────────────────────

async def send_invite(page, profile_url: str, note: str) -> tuple[bool, str]:
    try:
        await page.goto(profile_url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(_human_delay(4, 6))

        connect_btn = await page.query_selector('button[aria-label*="Connect"], button:has-text("Connect")')
        if not connect_btn or not await connect_btn.is_visible():
            more = await page.query_selector('button[aria-label*="More actions"], button:has-text("More")')
            if more:
                await more.click()
                await asyncio.sleep(_human_delay(1, 2))
                connect_btn = await page.query_selector('div[aria-label*="Connect"], div[role="menuitem"]:has-text("Connect")')
        if not connect_btn:
            return False, "no_connect_btn"

        await connect_btn.click()
        await asyncio.sleep(_human_delay(2, 3))

        add_note = await page.query_selector('button[aria-label*="Add a note"]')
        if add_note:
            await add_note.click()
            await asyncio.sleep(_human_delay(1, 2))
            field = await page.query_selector('textarea[name="message"], textarea#custom-message')
            if field:
                await field.fill(note)
                await asyncio.sleep(_human_delay(1, 2))

        send_btn = await page.query_selector('button[aria-label*="Send invitation"], button[aria-label="Send"], button:has-text("Send")')
        if not send_btn:
            return False, "no_send_btn"
        await send_btn.click()
        await asyncio.sleep(_human_delay(2, 4))
        return True, "sent"
    except Exception as e:
        return False, f"err:{str(e)[:60]}"


# ─── Main ─────────────────────────────────────────────────────────────────────

GOOGLE_INDIA_TARGET_ROLE = "Senior Machine Learning Engineer"
GOOGLE_INDIA_CONTEXT = "Google India's ML/AI engineering hiring"


async def main():
    print("=" * 70)
    print(f"GOOGLE INDIA HR OUTREACH — Sachin")
    print(f"  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"  Cap:  {MAX_CONNECTS} connect notes this run")
    print(f"  Scan: up to {SCAN_PROFILES} recruiter profiles")
    print("=" * 70)

    await ensure_table()

    proposals = []
    seen_profile = set()

    async with get_browser(headless=False) as page:
        if not await linkedin_login(page):
            logger.error("LinkedIn login failed — aborting")
            return

        for search_url in build_search_urls():
            if len(proposals) >= MAX_CONNECTS:
                break
            print(f"\nSearching: {search_url[:120]}...")
            profiles = await harvest_search_results(page, search_url, limit=SCAN_PROFILES)
            print(f"  → {len(profiles)} recruiter-shaped profiles found")
            for p in profiles:
                if len(proposals) >= MAX_CONNECTS:
                    break
                if p["url"] in seen_profile:
                    continue
                seen_profile.add(p["url"])
                if await already_sent(p["url"], days=90):
                    print(f"  ↪ already sent to {p['name']} — skip")
                    continue
                note = render_note(
                    first_name=p["name"],
                    role=GOOGLE_INDIA_TARGET_ROLE,
                    company="Google",
                    company_context=GOOGLE_INDIA_CONTEXT,
                    variant="v3_interest",
                )
                proposals.append({
                    "profile_url": p["url"],
                    "profile_name": p["name"],
                    "profile_role": p["role_at_company"],
                    "company": "Google",
                    "role": GOOGLE_INDIA_TARGET_ROLE,
                    "company_context": GOOGLE_INDIA_CONTEXT,
                    "variant": "v3_interest",
                    "note": note,
                    "note_chars": len(note),
                })
                print(f"  ✓ queued → {p['name']:<24} | {len(note)}c | {p['role_at_company'][:60]}")
            await asyncio.sleep(_human_delay(8, 14))  # gap between search URLs

        # Save queue
        queue_path = OUT_DIR / f"queue_google_india_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        queue_path.write_text(json.dumps(proposals, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n{'='*70}\nQueue: {len(proposals)} proposals (cap {MAX_CONNECTS})")
        print(f"Saved: {queue_path}")

        if DRY_RUN:
            print(f"\nDRY RUN — no invites sent. Re-run with --live to send.\n")
            for i, p in enumerate(proposals, 1):
                print(f"\n  #{i}  {p['profile_name']} ({p['profile_role'][:60]})")
                print(f"       chars={p['note_chars']}")
                print(f"       \"{p['note']}\"")
            return

        # LIVE
        sent_count = 0
        for p in proposals:
            print(f"\n→ Sending to {p['profile_name']} ({p['profile_role'][:60]})")
            ok, reason = await send_invite(page, p["profile_url"], p["note"])
            await record_note(
                profile_url=p["profile_url"],
                profile_name=p["profile_name"],
                company=p["company"],
                role=p["role"],
                variant=p["variant"],
                note=p["note"],
                status="sent" if ok else f"failed:{reason}",
            )
            if ok:
                sent_count += 1
                print(f"   ✓ sent ({sent_count}/{MAX_CONNECTS})")
            else:
                print(f"   ✗ failed: {reason}")
            await asyncio.sleep(_human_delay(90, 180))  # post-throttle-safe gap

        print(f"\n{'='*70}\nLIVE FINAL: {sent_count} connect notes sent to Google India recruiters\n{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
