"""Scan LinkedIn content/posts for "we're hiring + relocation/visa" announcements
matching Sachin's or Kritika's profile across Dubai/Japan/EU.

This is READ-ONLY browsing — no Connect, no DM, no Apply. The runner only opens
LinkedIn content-search URLs with Sachin's saved cookies and extracts post text.
After extraction, it uses `claude -p` to:
  - confirm the post is really hiring (vs. someone job-seeking)
  - extract role, location, relocation/visa signal, application method
  - score fit for Sachin and Kritika separately

Output: an HTML artifact for the user to review + apply manually via the post's
own application channel (external URL / email / "DM the recruiter").

We deliberately do NOT auto-apply or auto-DM here. Sachin's LinkedIn account is
currently in connection-throttle state (see linkedin_throttle memories) and any
write-mode automation risks escalation. This script's job is to surface
high-quality candidates; user decides what to act on.
"""

import asyncio
import json
import logging
import re
import sys
import shutil
import os
import urllib.parse
from datetime import datetime
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scan-hiring-posts")

COOKIES_PATH = BASE_DIR / "data" / "linkedin_cookies.json"
OUT_DIR = BASE_DIR / "data" / "hiring_posts"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Search queries — each targets a city + hiring keywords + sponsorship signal.
# Past-week filter ensures we get fresh posts.
SEARCH_QUERIES = [
    ("hiring AI engineer Dubai relocation", "Dubai"),
    ("hiring machine learning engineer Dubai visa sponsorship", "Dubai"),
    ("hiring AI engineer Tokyo relocation visa", "Tokyo"),
    ("hiring machine learning Tokyo visa sponsorship", "Tokyo"),
    ("hiring AI engineer Berlin relocation EU blue card", "Berlin"),
    ("hiring machine learning Berlin visa sponsorship", "Berlin"),
    ("hiring AI engineer Amsterdam relocation visa", "Amsterdam"),
    ("hiring machine learning Netherlands visa sponsorship", "Amsterdam"),
    ("hiring AI engineer London visa sponsorship", "London"),
    ("hiring senior ML engineer Singapore visa", "Singapore"),
]

# Words signaling RELOCATION / VISA sponsorship in post text. We only keep posts
# that contain at least one of these to filter out noise.
RELOCATION_SIGNALS = [
    "visa sponsorship", "sponsor visa", "relocation package", "relocation support",
    "we sponsor", "blue card", "eu blue card", "highly skilled migrant",
    "relocate", "relocating", "visa support", "international candidates",
    "global mobility", "work permit", "employment pass",
]

# Hard-filter terms — if any appear in post, SKIP (visa-required-NO style posts).
NEGATIVE_SIGNALS = [
    "no visa sponsorship", "no sponsorship", "must be eligible to work",
    "must have right to work", "no relocation", "us citizens only",
    "citizens only", "must be a citizen", "permanent resident only",
]

MAX_POSTS_PER_QUERY = 15
TOTAL_QUERIES_CAP = 10  # don't burn the whole LinkedIn session


def _load_cookies() -> list:
    if not COOKIES_PATH.exists():
        return []
    try:
        return json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _has_relocation_signal(text: str) -> tuple[bool, list[str]]:
    """Return (any_match, list_of_matched_signal_phrases)."""
    if not text:
        return False, []
    t = text.lower()
    hits = [s for s in RELOCATION_SIGNALS if s in t]
    return bool(hits), hits


def _has_negative_signal(text: str) -> tuple[bool, list[str]]:
    if not text:
        return False, []
    t = text.lower()
    hits = [s for s in NEGATIVE_SIGNALS if s in t]
    return bool(hits), hits


async def harvest_posts(page, query: str, location_hint: str) -> list[dict]:
    """Open LinkedIn content search for `query`, scroll, extract posts."""
    url = (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={urllib.parse.quote(query)}"
        "&datePosted=%22past-week%22"
        "&origin=FACETED_SEARCH"
        "&sortBy=%22date_posted%22"
    )
    logger.info(f"Search ({location_hint}): {query}")
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning(f"  goto failed: {str(e)[:120]}")
        return []
    await asyncio.sleep(5)
    # Scroll to load more posts
    for _ in range(5):
        await page.keyboard.press("End")
        await asyncio.sleep(2)

    posts = await page.evaluate("""(loc) => {
        // LinkedIn post containers: feed-shared-update-v2 or similar
        const articles = Array.from(document.querySelectorAll(
            'div.feed-shared-update-v2, [data-urn*="urn:li:activity:"], article'
        ));
        const out = [];
        const seen = new Set();
        for (const art of articles) {
            // Get URN/activity ID
            const urn = art.getAttribute('data-urn') || '';
            const activityMatch = urn.match(/urn:li:activity:(\\d+)/) ||
                                  (art.innerHTML.match(/urn:li:activity:(\\d+)/) || []);
            const activityId = activityMatch ? activityMatch[1] : '';

            // Find post URL anchor — usually has /feed/update/urn:li:activity:
            let postUrl = '';
            const linkEl = art.querySelector('a[href*="/feed/update/urn:li:activity:"]') ||
                          art.querySelector('a[href*="/posts/"]');
            if (linkEl) postUrl = linkEl.getAttribute('href');
            if (!postUrl && activityId) postUrl = `https://www.linkedin.com/feed/update/urn:li:activity:${activityId}/`;
            if (!postUrl) continue;
            if (seen.has(postUrl)) continue;
            seen.add(postUrl);

            // Author
            const authorEl = art.querySelector('span[aria-hidden="true"]') ||
                            art.querySelector('a[href*="/in/"] span');
            const author = authorEl ? (authorEl.textContent || '').trim() : '';
            const authorLink = (art.querySelector('a[href*="/in/"]') || {}).href || '';

            // Post text — the main content area
            const textEl = art.querySelector(
                '.feed-shared-update-v2__description, .feed-shared-text, ' +
                '.update-components-text, [class*="description"]'
            );
            let text = textEl ? (textEl.textContent || '') : (art.textContent || '');
            text = text.replace(/\\s+/g, ' ').trim();
            if (text.length < 80) continue;  // skip noise

            out.push({
                postUrl: postUrl.startsWith('http') ? postUrl : `https://www.linkedin.com${postUrl}`,
                activityId,
                author,
                authorLink: authorLink.split('?')[0],
                text: text.slice(0, 3000),
                location_hint: loc,
            });
            if (out.length >= 25) return out;
        }
        return out;
    }""", location_hint)
    logger.info(f"  → {len(posts)} posts found")
    return posts


def _filter_post(post: dict) -> tuple[bool, dict]:
    """Apply hard relocation filter + negative filter. Returns (keep, meta)."""
    text = post.get("text", "")
    has_reloc, reloc_hits = _has_relocation_signal(text)
    has_neg, neg_hits = _has_negative_signal(text)
    if not has_reloc:
        return False, {"reason": "no_relocation_signal"}
    if has_neg:
        return False, {"reason": "negative_signal", "neg_hits": neg_hits}
    return True, {"reloc_hits": reloc_hits[:3]}


# ─── claude -p subprocess for fit scoring ─────────────────────────────────────

async def _claude_p(prompt: str, timeout: int = 60) -> str:
    """Run prompt through `claude -p` subprocess (uses OAuth, no API key)."""
    claude_path = shutil.which("claude.cmd") or shutil.which("claude")
    if not claude_path:
        return ""
    try:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_shell(
                f'"{claude_path}" -p --output-format text --dangerously-skip-permissions',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                claude_path, "-p", "--output-format", "text", "--dangerously-skip-permissions",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception as e:
        logger.warning(f"  claude -p failed: {str(e)[:80]}")
        return ""


SACHIN_BRIEF = (
    "Sachin (Candidate A): 8 yrs senior ML at TrueBalance (regulated fintech, India). "
    "Specialty: LangGraph + RAG (Weaviate/Qdrant), Claude/GPT/Llama, multi-agent LLM. "
    "Targets: Berlin, Dubai, Tokyo, Singapore — VISA SPONSORSHIP REQUIRED."
)
KRITIKA_BRIEF = (
    "Kritika (Candidate B): 7 yrs Senior AI Engineer (India). "
    "Specialty: GenAI, LLM, LangChain, RAG, AWS, Docker, FastAPI. "
    "Targets: India primary + international remote / sponsorship-friendly."
)

POST_ANALYSIS_PROMPT = """Read this LinkedIn post and answer concisely. ONE LINE PER FIELD.

POST TEXT:
{text}

POST LOCATION HINT: {loc}

OUTPUT EXACTLY this format (lines that don't apply, leave value blank):
ROLE: <job title or "N/A">
LOCATION: <city/country>
RELOCATION: <yes/no/unclear>  -- yes only if post EXPLICITLY mentions relocation/visa sponsorship
APPLY_METHOD: <"url:" then the URL | "email:" then the address | "DM" | "comment" | "unknown">
APPLY_TARGET: <the URL or email or just "DM the post author">
SACHIN_FIT: <0-10 score>  -- {sachin_brief}
KRITIKA_FIT: <0-10 score>  -- {kritika_brief}
SUMMARY: <one short sentence why it's a good or bad match>
"""


def _parse_analysis(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().upper()
        val = val.strip()
        out[key] = val
    return out


async def analyze_post(post: dict) -> dict:
    prompt = POST_ANALYSIS_PROMPT.format(
        text=post["text"][:2200],
        loc=post["location_hint"],
        sachin_brief=SACHIN_BRIEF,
        kritika_brief=KRITIKA_BRIEF,
    )
    reply = await _claude_p(prompt)
    parsed = _parse_analysis(reply)
    return parsed


# ─── HTML output ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<title>Hiring Posts Scan — Sachin + Kritika</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
          background: #fafafa; margin: 0; padding: 24px 16px 60px; color: #1a1a1a; }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 22px; }}
  .stats {{ background: #fff; border: 1px solid #e6e6e6; border-radius: 10px;
            padding: 12px 16px; margin-bottom: 22px; font-size: 13px;
            display: flex; gap: 24px; flex-wrap: wrap; }}
  .stats b {{ color: #1a73e8; }}
  .card {{ background: #fff; border: 1px solid #e6e6e6; border-radius: 10px;
           padding: 14px 16px 12px; margin-bottom: 14px; }}
  .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,0.04); }}
  .row {{ display: flex; justify-content: space-between; align-items: flex-start;
          gap: 12px; margin-bottom: 8px; }}
  .role {{ font-size: 15px; font-weight: 600; }}
  .loc {{ font-size: 12px; color: #666; }}
  .scores {{ display: flex; gap: 8px; flex-shrink: 0; }}
  .score {{ font-size: 11px; padding: 3px 8px; border-radius: 10px;
            background: #f1f3f4; color: #444; font-weight: 600; }}
  .score.hi {{ background: #d4f4dd; color: #1f7a3a; }}
  .score.mid {{ background: #fff4cc; color: #8a5a00; }}
  .summary {{ font-size: 13px; color: #333; margin: 6px 0 8px; }}
  .meta {{ font-size: 12px; color: #555; margin-bottom: 8px; }}
  .meta a {{ color: #1a73e8; text-decoration: none; }}
  .meta a:hover {{ text-decoration: underline; }}
  .actions {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
  .btn {{ padding: 5px 11px; border: 1px solid #d1d5db; background: #fff;
          color: #1a73e8; border-radius: 5px; font-size: 12px; text-decoration: none; }}
  .btn:hover {{ background: #1a73e8; color: #fff; border-color: #1a73e8; }}
  .reloc-hits {{ font-size: 11px; color: #1f7a3a; margin-top: 4px; }}
  .none {{ text-align: center; padding: 40px; color: #888; }}
</style>
<div class="container">
  <h1>Hiring Posts — relocation/visa-sponsorship signals</h1>
  <div class="subtitle">{stamp} · queries: {n_queries} · raw posts: {raw} · matched: {matched}</div>
  <div class="stats">
    <span>Dubai: <b>{dubai}</b></span>
    <span>Tokyo: <b>{tokyo}</b></span>
    <span>Berlin: <b>{berlin}</b></span>
    <span>Amsterdam: <b>{amsterdam}</b></span>
    <span>Other: <b>{other}</b></span>
  </div>
  {cards}
</div>
"""


def _score_class(s: str) -> str:
    try:
        n = int(re.match(r"(\d+)", s.strip()).group(1))
    except Exception:
        return ""
    if n >= 7: return " hi"
    if n >= 4: return " mid"
    return ""


def _card_html(post: dict, analysis: dict, meta: dict) -> str:
    role = analysis.get("ROLE", "Unknown role")[:80]
    loc = analysis.get("LOCATION", post.get("location_hint", ""))[:60]
    summary = analysis.get("SUMMARY", "")[:240]
    sachin = analysis.get("SACHIN_FIT", "?")
    kritika = analysis.get("KRITIKA_FIT", "?")
    apply_method = analysis.get("APPLY_METHOD", "unknown")
    apply_target = analysis.get("APPLY_TARGET", "")
    author = post.get("author", "")[:60]
    author_link = post.get("authorLink", "")
    post_url = post.get("postUrl", "")
    reloc_hits = meta.get("reloc_hits", [])

    sclass = _score_class(sachin)
    kclass = _score_class(kritika)

    # Apply link if URL/email
    apply_btn = ""
    if apply_target.startswith("http"):
        apply_btn = f'<a class="btn" href="{apply_target}" target="_blank">Apply →</a>'
    elif "@" in apply_target and apply_target.split("@")[-1]:
        apply_btn = f'<a class="btn" href="mailto:{apply_target}">Email {apply_target}</a>'

    parts = []
    parts.append('<div class="card">')
    parts.append(f'<div class="row"><div><div class="role">{role}</div>'
                 f'<div class="loc">{loc} · via {apply_method}</div></div>')
    parts.append(f'<div class="scores">'
                 f'<span class="score{sclass}">Sachin {sachin}/10</span>'
                 f'<span class="score{kclass}">Kritika {kritika}/10</span>'
                 f'</div></div>')
    if summary:
        parts.append(f'<div class="summary">{summary}</div>')
    parts.append('<div class="meta">')
    if author:
        if author_link:
            parts.append(f'Posted by <a href="{author_link}" target="_blank">{author}</a> · ')
        else:
            parts.append(f'Posted by {author} · ')
    parts.append(f'<a href="{post_url}" target="_blank">Open post</a></div>')
    if reloc_hits:
        parts.append(f'<div class="reloc-hits">✓ {" · ".join(reloc_hits)}</div>')
    parts.append('<div class="actions">')
    if apply_btn:
        parts.append(apply_btn)
    parts.append(f'<a class="btn" href="{post_url}" target="_blank">View post</a>')
    if author_link:
        parts.append(f'<a class="btn" href="{author_link}" target="_blank">Author profile</a>')
    parts.append('</div></div>')
    return "".join(parts)


async def main():
    print("=" * 70)
    print("LINKEDIN HIRING POSTS SCAN — Sachin + Kritika")
    print("=" * 70)

    cookies = _load_cookies()
    if not cookies:
        print(f"❌ No cookies at {COOKIES_PATH}")
        return

    queries = SEARCH_QUERIES[:TOTAL_QUERIES_CAP]
    all_raw_posts: list[dict] = []
    matched: list[tuple[dict, dict]] = []  # (post, filter_meta)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            locale="en-US",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        if HAS_STEALTH:
            try: await Stealth().apply_stealth_async(page)
            except: pass

        for query, loc in queries:
            posts = await harvest_posts(page, query, loc)
            all_raw_posts.extend(posts)
            for p in posts:
                keep, meta = _filter_post(p)
                if keep:
                    matched.append((p, meta))
            await asyncio.sleep(3)

        await browser.close()

    # Dedupe by postUrl
    seen = set()
    deduped: list[tuple[dict, dict]] = []
    for p, m in matched:
        if p["postUrl"] in seen:
            continue
        seen.add(p["postUrl"])
        deduped.append((p, m))
    print(f"\nRaw posts: {len(all_raw_posts)} · with relocation signal: {len(deduped)}")

    # Analyze with claude -p (parallel, but limit concurrency)
    sem = asyncio.Semaphore(3)
    async def _bounded_analyze(p):
        async with sem:
            return await analyze_post(p)

    analyses = await asyncio.gather(*(_bounded_analyze(p) for p, _ in deduped))

    # Filter to only posts where claude confirms hiring + relocation
    final: list[tuple[dict, dict, dict]] = []
    for (post, meta), analysis in zip(deduped, analyses):
        reloc = analysis.get("RELOCATION", "").lower()
        if "yes" not in reloc:
            continue  # claude says no relocation despite our keyword match — drop
        # Also require min fit score >= 4 for either candidate
        try:
            sf = int(re.match(r"(\d+)", analysis.get("SACHIN_FIT", "0")).group(1))
        except Exception:
            sf = 0
        try:
            kf = int(re.match(r"(\d+)", analysis.get("KRITIKA_FIT", "0")).group(1))
        except Exception:
            kf = 0
        if max(sf, kf) < 4:
            continue
        final.append((post, meta, analysis))

    # Sort by max fit score (desc)
    def _max_fit(t):
        a = t[2]
        try: sf = int(re.match(r"(\d+)", a.get("SACHIN_FIT", "0")).group(1))
        except: sf = 0
        try: kf = int(re.match(r"(\d+)", a.get("KRITIKA_FIT", "0")).group(1))
        except: kf = 0
        return max(sf, kf)
    final.sort(key=_max_fit, reverse=True)
    print(f"After fit-filter (≥4 for either candidate + claude confirms relocation): {len(final)}")

    # Bucket by location for stats
    buckets = {"dubai": 0, "tokyo": 0, "berlin": 0, "amsterdam": 0, "other": 0}
    for post, _meta, _ in final:
        lh = (post.get("location_hint") or "").lower()
        if lh in buckets:
            buckets[lh] += 1
        else:
            buckets["other"] += 1

    # Build HTML
    cards = "".join(_card_html(p, a, m) for p, m, a in final)
    if not cards:
        cards = '<div class="none">No matching posts found. Try again later or relax the relocation filter.</div>'

    html = HTML_TEMPLATE.format(
        stamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        n_queries=len(queries),
        raw=len(all_raw_posts),
        matched=len(final),
        dubai=buckets["dubai"], tokyo=buckets["tokyo"],
        berlin=buckets["berlin"], amsterdam=buckets["amsterdam"],
        other=buckets["other"],
        cards=cards,
    )
    out_html = OUT_DIR / f"hiring_posts_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.html"
    out_html.write_text(html, encoding="utf-8")
    # Also save raw JSON for debugging
    out_json = OUT_DIR / f"hiring_posts_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    out_json.write_text(json.dumps([
        {"post": p, "filter_meta": m, "analysis": a} for p, m, a in final
    ], indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"HTML: {out_html}")
    print(f"JSON: {out_json}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
