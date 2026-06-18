"""Send capability-matched LinkedIn connect notes to HR/recruiters at companies Sachin applied to.

Flow:
  1. Login to Sachin's LinkedIn (cookies → creds).
  2. Harvest "Applied jobs" tracker (last N applications).
  3. For each applied job: visit job page, scrape JD excerpt, find hiring team profiles.
  4. For each HR/recruiter profile:
       a. Generate company_context via claude -p (with caching).
       b. Pick variant per workflow design (HM/eng vs recruiter vs agent/eval).
       c. Render note ≤300 chars.
       d. Dedupe vs `linkedin_notes_sent` SQLite table.
       e. Throttle (90-180s random gap, 5-8/day cap by default).
       f. If --live, click Connect → Add note → Send; otherwise dry-run preview.

Flags:
  --max=N         max connects this run (default 6; bumps allowed but throttle memory says 5-8 post-warning)
  --live          actually send (default = dry-run; prints rendered notes only)
  --jobs=N        how many recent applied jobs to scan for HR (default 25)
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
from src.hr_connect import build_note_for, generate_company_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("connect-hr")

DB_PATH = BASE_DIR / "data" / "jobs.db"
OUT_DIR = BASE_DIR / "data" / "hr_connect"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _arg_int(flag: str, default: int) -> int:
    for arg in sys.argv:
        if arg.startswith(flag + "="):
            try: return int(arg.split("=", 1)[1])
            except: pass
    return default


DRY_RUN = "--live" not in sys.argv
MAX_CONNECTS = _arg_int("--max", 6)
JOBS_TO_SCAN = _arg_int("--jobs", 25)


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
    """Return True if we sent a note to this profile in the last `days` days (live only)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT 1 FROM linkedin_notes_sent WHERE profile_url=? AND dry_run=0 AND sent_at>? LIMIT 1",
            (profile_url, cutoff),
        )
        row = await cur.fetchone()
        return bool(row)


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


# ─── LinkedIn harvest helpers ─────────────────────────────────────────────────

APPLIED_TRACKER_URLS = [
    "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED",
    "https://www.linkedin.com/jobs/tracker/applied/",
    "https://www.linkedin.com/my-items/posts/saved-jobs/",
]


async def harvest_applied_jobs(page, limit: int = 25) -> list[dict]:
    """Try several LinkedIn URLs that list the user's recent applied jobs."""
    for url in APPLIED_TRACKER_URLS:
        try:
            logger.info(f"Trying applied-tracker URL: {url}")
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(_human_delay(4, 6))
            for _ in range(5):
                await page.keyboard.press("End")
                await asyncio.sleep(_human_delay(1.5, 2.5))
            jobs = await page.evaluate("""() => {
                const anchors = Array.from(document.querySelectorAll('a[href*="/jobs/view/"]'));
                const seen = new Set();
                const out = [];
                for (const a of anchors) {
                    const m = a.getAttribute('href').match(/\\/jobs\\/view\\/(\\d+)/);
                    if (!m) continue;
                    const id = m[1];
                    if (seen.has(id)) continue;
                    seen.add(id);
                    let card = a.closest('li, article, [class*="entity"], [class*="card"]') || a;
                    const text = (card.textContent || '').replace(/\\s+/g, ' ').trim();
                    out.push({id, text: text.slice(0, 400), url: `https://www.linkedin.com/jobs/view/${id}/`});
                }
                return out;
            }""")
            if jobs:
                logger.info(f"  Harvested {len(jobs)} applied/saved jobs from {url}")
                return jobs[:limit]
        except Exception as e:
            logger.warning(f"  Failed {url}: {str(e)[:120]}")
            continue
    return []


async def fetch_job_context(page, job_url: str) -> tuple[str, str, str, str, list[dict]]:
    """Visit a job page; return (title, company, location, jd_excerpt, hiring_team_profiles).

    LinkedIn 2026 uses obfuscated class names — rely on `document.title`, /company/ anchors,
    and full-text scan for the hiring-section profile.
    """
    title, company, location, jd_excerpt = "", "", "", ""
    profiles: list[dict] = []

    try:
        await page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
        await asyncio.sleep(_human_delay(3, 5))
        await page.keyboard.press("End")
        await asyncio.sleep(_human_delay(1.5, 2.5))

        # Pull title from document.title — most reliable.
        # Pattern: "<Role> | <Company> | LinkedIn"  (sometimes "<Role> at <Company> | LinkedIn")
        page_title = await page.title()
        parts = [p.strip() for p in page_title.split(" | ") if p.strip()]
        if parts and parts[-1].lower() == "linkedin":
            parts = parts[:-1]
        if len(parts) >= 2:
            company = parts[-1]
            title = " | ".join(parts[:-1])
        elif len(parts) == 1:
            # Try "Role at Company" pattern
            m = re.match(r"^(.+?)\s+at\s+(.+)$", parts[0])
            if m:
                title, company = m.group(1).strip(), m.group(2).strip()
            else:
                title = parts[0]

        # Company fallback: first /company/ anchor text
        if not company:
            company = await page.evaluate("""() => {
                const a = document.querySelector('a[href*="/company/"]');
                return a ? (a.textContent || '').trim() : '';
            }""")

        # JD excerpt: scan body text from "About the job"/"Job description" marker
        jd_excerpt = await page.evaluate("""() => {
            const text = document.body ? document.body.innerText : '';
            const m = text.match(/(About the job|About this role|Job description|Job Description|About the Role)/i);
            if (m) return text.slice(m.index, m.index + 1800);
            return text.slice(0, 2000);
        }""")
        jd_excerpt = (jd_excerpt or "")[:1500]
        title = (title or "")[:120]
        company = (company or "")[:80]

        # Hiring team profile(s): traverse sections that mention hiring/recruiter, grab /in/ anchors
        profile_data = await page.evaluate("""() => {
            const sections = Array.from(document.querySelectorAll('section, div'))
                .filter(s => /hiring|recruiter|talent|posted by|meet the/i.test(s.textContent || ''));
            const seen = new Set();
            const matches = [];
            for (const s of sections) {
                const txt = (s.textContent || '').replace(/\\s+/g, ' ').trim();
                if (txt.length > 6000) continue;  // skip whole-page parents
                const links = s.querySelectorAll('a[href*="/in/"]');
                for (const a of links) {
                    const href = a.getAttribute('href').split('?')[0];
                    if (seen.has(href)) continue;
                    seen.add(href);
                    const card = a.closest('div, li, section') || a;
                    const block = (card.textContent || '').replace(/\\s+/g, ' ').trim();
                    matches.push({
                        url: href.startsWith('http') ? href : 'https://www.linkedin.com' + href,
                        block: block.slice(0, 300),
                        anchorText: (a.textContent || '').trim().slice(0, 80),
                    });
                    if (matches.length >= 5) return matches;
                }
            }
            return matches;
        }""")

        for p in profile_data:
            # Prefer anchorText (cleaner) over block parsing
            atext = p.get("anchorText") or ""
            # anchorText typically: "FirstName LastName• 3rd..." — split on '•' and bullet chars
            primary = re.split(r"[•·]|3rd|2nd|1st", atext, maxsplit=1)[0].strip()
            name_match = re.match(r"([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})", primary)
            name = name_match.group(1) if name_match else ""
            # role hint: block text after the name, before "Connect"/"Follow"/etc
            block = p["block"]
            role_block = block[len(name):] if name and name in block else block
            role_block = re.split(r"\b(Connect|Follow|View profile|See more|Show all)\b|[•·]", role_block, maxsplit=1)[0]
            role_block = role_block.strip(" ·•,.")[:120]
            profiles.append({
                "url": p["url"],
                "name": name or "there",
                "role_at_company": role_block,
            })
    except Exception as e:
        logger.warning(f"  fetch_job_context error: {str(e)[:120]}")

    return title, company, location, jd_excerpt, profiles


# ─── Sending ──────────────────────────────────────────────────────────────────

async def send_invite(page, profile_url: str, note: str) -> tuple[bool, str]:
    """Returns (success, reason)."""
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

async def main():
    print("=" * 70)
    print(f"HR CONNECT OUTREACH — Sachin")
    print(f"  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"  Cap:  {MAX_CONNECTS} connects this run")
    print(f"  Scan: {JOBS_TO_SCAN} most-recent applied jobs")
    print("=" * 70)

    await ensure_table()

    async with get_browser(headless=False) as page:
        if not await linkedin_login(page):
            logger.error("LinkedIn login failed — aborting")
            return

        applied = await harvest_applied_jobs(page, limit=JOBS_TO_SCAN)
        if not applied:
            print("No applied jobs found on LinkedIn tracker. Has Sachin applied via Easy Apply recently?")
            return

        proposals: list[dict] = []  # to-send queue (after dedupe)
        seen_company_role = set()

        for i, job in enumerate(applied, 1):
            if len(proposals) >= MAX_CONNECTS:
                break
            print(f"\n[{i}/{len(applied)}] Inspecting {job['url']}")
            title, company, location, jd_excerpt, profiles = await fetch_job_context(page, job["url"])
            print(f"   Role: {title or '(unknown)'}  @  {company or '(unknown)'}")
            if not company:
                print("   ✗ company unknown — skip")
                continue
            cr_key = f"{(company or '').lower()}|{(title or '').lower()}"
            if cr_key in seen_company_role:
                print("   ✗ (company, role) already seen this run — skip")
                continue
            seen_company_role.add(cr_key)

            if not profiles:
                print("   ✗ no hiring team profiles surfaced — skip")
                continue

            # Generate company context once per company; cached in src.hr_connect
            cc = await generate_company_context(company, jd_excerpt)
            print(f"   Company context: {cc}")

            for p in profiles[:2]:  # at most 2 per role
                if len(proposals) >= MAX_CONNECTS:
                    break
                if await already_sent(p["url"], days=90):
                    print(f"   ↪ already sent to {p['name']} ({p['url']}) — skip")
                    continue
                # Build note using role title, but variant uses recipient's title for HM detection
                variant_role_hint = p.get("role_at_company") or title
                note, variant, _ = await build_note_for(
                    first_name=p["name"], role=title, company=company, jd_excerpt=jd_excerpt
                )
                # override variant if recipient is HM
                from src.hr_connect import pick_variant, render_note
                v = pick_variant(variant_role_hint, cc)
                if v != variant:
                    variant = v
                    note = render_note(p["name"], title, company, cc, variant)

                proposals.append({
                    "profile_url": p["url"],
                    "profile_name": p["name"],
                    "profile_role": p.get("role_at_company", ""),
                    "company": company,
                    "role": title,
                    "company_context": cc,
                    "variant": variant,
                    "note": note,
                    "note_chars": len(note),
                })
                print(f"   ✓ proposed → {p['name']:<24} | {variant:<12} | {len(note)}c")
                print(f"      \"{note}\"")
            await asyncio.sleep(_human_delay(3, 6))

        print(f"\n{'='*70}\nProposals queued: {len(proposals)} (cap {MAX_CONNECTS})\n{'='*70}")

        # Save proposals queue
        queue_path = OUT_DIR / f"queue_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        queue_path.write_text(json.dumps(proposals, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Queue saved: {queue_path}")

        if DRY_RUN:
            print("\nDRY RUN — no invites sent. Re-run with --live to send.")
            print("First 5 rendered notes:")
            for i, p in enumerate(proposals[:5], 1):
                print(f"\n  #{i}  {p['profile_name']} ({p['profile_role'][:50]})")
                print(f"       at {p['company']}  re: {p['role']}")
                print(f"       variant={p['variant']}  chars={p['note_chars']}")
                print(f"       \"{p['note']}\"")
            return

        # LIVE
        sent_count = 0
        for p in proposals:
            print(f"\n→ Sending to {p['profile_name']} ({p['profile_role'][:50]}) @ {p['company']}")
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

        print(f"\n{'='*70}\nLIVE FINAL: {sent_count} connect notes sent to HR/recruiters\n{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
