"""Verify visa-sponsorship claims for each company on the curated outreach list.

For each company:
  1. Open careers page in Playwright (no auth needed)
  2. Harvest job-detail URLs matching AI/ML/Engineer keywords
  3. Open up to 4 JD pages each
  4. Search the full JD text for explicit sponsorship phrases
  5. Capture the exact sentence(s) containing each phrase
  6. Grade the company:
       - GREEN: ≥2 distinct JDs contain explicit sponsorship language
       - YELLOW: 1 JD contains it OR aggregator confirms
       - RED: 0 JDs contain it AND we have no aggregator confirmation

Output: JSON + HTML artifact with per-company verdict + quoted evidence.

This is read-only public-page browsing. No LinkedIn cookies. No account at risk.
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from src.config import BASE_DIR

OUT_DIR = BASE_DIR / "data" / "visa_verification"
OUT_DIR.mkdir(parents=True, exist_ok=True)


SPONSORSHIP_PHRASES = [
    # Strong signals
    "visa sponsorship",
    "sponsor your visa",
    "we sponsor visas",
    "we sponsor",
    "blue card",
    "eu blue card",
    "highly skilled migrant",
    "30% ruling",
    "relocation package",
    "relocation support",
    "relocation assistance",
    "international candidates",
    "global mobility",
    "employment pass",
    "work permit",
    "uae visa",
    "uae employment visa",
    "japan visa",
    "germany work visa",
    "netherlands visa",
    # Slightly weaker but still positive
    "willing to sponsor",
    "happy to sponsor",
    "open to sponsor",
    "consider visa",
    "relocate to",
    "will relocate",
]

# Anti-signals (negate the company's claim if found in the same JD)
NEGATIVE_PHRASES = [
    "no visa sponsorship",
    "we do not sponsor",
    "we cannot sponsor",
    "must have right to work",
    "must be eligible to work",
    "citizens only",
    "permanent resident only",
    "no relocation",
]

ROLE_KEYWORDS = [
    "machine learning", "ml engineer", "ai engineer", "data scientist",
    "applied scientist", "research engineer", "software engineer",
    "llm", "genai", "deep learning", "nlp",
]


COMPANIES = [
    {"name": "Tether", "url": "https://tether.io/careers/",
     "anchor_attr": "href", "anchor_includes": ["/careers/"]},
    {"name": "Binance", "url": "https://www.binance.com/en/careers",
     "anchor_includes": ["/careers/"]},
    {"name": "Sakana AI", "url": "https://sakana.ai/careers/",
     "anchor_includes": ["careers", "job"]},
    {"name": "Rakuten", "url": "https://global.rakuten.com/corp/careers/positions/",
     "anchor_includes": ["careers", "position", "job"]},
    {"name": "Databricks", "url": "https://www.databricks.com/company/careers/open-positions?location=Berlin",
     "anchor_includes": ["/careers/", "/jobs/"]},
    {"name": "Delivery Hero", "url": "https://careers.deliveryhero.com/global/en/search-results?keywords=machine+learning",
     "anchor_includes": ["/job/", "/jobs/"]},
    {"name": "Zalando", "url": "https://jobs.zalando.com/en/jobs/?categories=engineering&locations=Berlin",
     "anchor_includes": ["/jobs/", "/de/jobs/", "/en/jobs/"]},
    {"name": "Google Berlin/Munich", "url": "https://www.google.com/about/careers/applications/jobs/results?q=machine+learning&location=Berlin%2C+Germany&location=Munich%2C+Germany",
     "anchor_includes": ["/jobs/results/"]},
    {"name": "SAP", "url": "https://jobs.sap.com/search/?q=machine+learning&locationsearch=Germany",
     "anchor_includes": ["/job/"]},
    {"name": "Personio", "url": "https://www.personio.com/careers/jobs/?disciplines=engineering",
     "anchor_includes": ["/jobs/", "/careers/jobs/"]},
    {"name": "Booking.com", "url": "https://careers.booking.com/jobs",
     "anchor_includes": ["/jobs/"]},
    {"name": "Adyen", "url": "https://careers.adyen.com/vacancies?category=Tech",
     "anchor_includes": ["/vacancies/", "/vacancy/"]},
]

MAX_JDS_PER_COMPANY = 4
PAGE_TIMEOUT_MS = 25000


def _has_role_keyword(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ROLE_KEYWORDS)


def _find_sponsorship_evidence(jd_text: str) -> tuple[list[str], list[str]]:
    """Return (positive_evidence_sentences, negative_evidence_sentences)."""
    t = jd_text.lower()
    # Split into sentences (rough)
    sentences = re.split(r"(?<=[.!?])\s+", jd_text)
    pos, neg = [], []
    for sent in sentences:
        sl = sent.lower()
        if any(p in sl for p in NEGATIVE_PHRASES):
            neg.append(sent.strip()[:280])
        elif any(p in sl for p in SPONSORSHIP_PHRASES):
            pos.append(sent.strip()[:280])
    # Dedupe
    seen = set()
    pos_unique = []
    for s in pos:
        k = s[:80].lower()
        if k not in seen:
            seen.add(k)
            pos_unique.append(s)
    return pos_unique[:5], neg[:3]


async def harvest_jd_links(page, company) -> list[dict]:
    """Visit careers page, scroll, return list of {url, anchor_text} for likely JDs."""
    try:
        await page.goto(company["url"], timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception as e:
        return []
    await asyncio.sleep(4)
    # Scroll to load more
    for _ in range(4):
        await page.keyboard.press("End")
        await asyncio.sleep(1.5)
    anchor_filter = company.get("anchor_includes") or [""]
    raw = await page.evaluate("""(filters) => {
        const out = [];
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        const seen = new Set();
        for (const a of anchors) {
            const href = a.getAttribute('href') || '';
            if (!href || href.startsWith('#') || href.startsWith('mailto:')) continue;
            if (!filters.some(f => !f || href.toLowerCase().includes(f.toLowerCase()))) continue;
            const text = (a.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length < 5) continue;
            const full = href.startsWith('http') ? href : new URL(href, location.href).href;
            const key = full.split('#')[0];
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({url: key, text: text.slice(0, 180)});
        }
        return out;
    }""", anchor_filter)
    # Filter by role keywords in anchor text
    role_filtered = [r for r in raw if _has_role_keyword(r["text"])]
    if not role_filtered:
        # Fall back to all anchors that look like job links — even if text didn't trigger
        # keyword (some sites have generic "Apply now" buttons that hide the role)
        role_filtered = raw[:20]
    return role_filtered[:MAX_JDS_PER_COMPANY * 3]


async def fetch_jd_evidence(page, jd_url: str) -> dict:
    """Open one JD URL, return dict with text excerpts + sponsorship evidence."""
    try:
        await page.goto(jd_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception as e:
        return {"url": jd_url, "error": f"goto:{str(e)[:80]}", "pos": [], "neg": [], "text_len": 0, "title": ""}
    await asyncio.sleep(3)
    # Scroll a bit to trigger any lazy-loaded JD content
    for _ in range(2):
        await page.keyboard.press("End")
        await asyncio.sleep(1)
    text_data = await page.evaluate("""() => {
        const title = document.title || '';
        const body = document.body ? document.body.innerText : '';
        return {title: title.slice(0, 160), text: body.slice(0, 20000)};
    }""")
    title = text_data.get("title", "")
    text = text_data.get("text", "")
    pos, neg = _find_sponsorship_evidence(text)
    return {"url": jd_url, "title": title, "text_len": len(text), "pos": pos, "neg": neg}


def grade_company(jds_with_evidence: list[dict]) -> str:
    """GREEN if ≥2 JDs have positive evidence and no negatives;
       YELLOW if 1 JD has positive evidence OR mixed;
       RED otherwise."""
    pos_jds = [j for j in jds_with_evidence if j.get("pos")]
    neg_jds = [j for j in jds_with_evidence if j.get("neg")]
    if len(pos_jds) >= 2 and not neg_jds:
        return "GREEN"
    if pos_jds and not neg_jds:
        return "YELLOW"
    if pos_jds and neg_jds:
        return "YELLOW"  # mixed signals
    return "RED"


async def verify_one(ctx, company: dict) -> dict:
    page = await ctx.new_page()
    try:
        print(f"\n[{company['name']}] careers: {company['url']}")
        links = await harvest_jd_links(page, company)
        print(f"  harvested {len(links)} candidate JD links")
        if not links:
            return {"company": company["name"], "careers_url": company["url"],
                    "grade": "UNKNOWN", "reason": "no_jd_links_found", "jds": []}
        # Open the first MAX_JDS JDs
        jds = []
        for link in links[:MAX_JDS_PER_COMPANY]:
            ev = await fetch_jd_evidence(page, link["url"])
            ev["anchor_text"] = link["text"]
            jds.append(ev)
            print(f"    JD: {link['text'][:60]:60s} pos={len(ev['pos'])} neg={len(ev['neg'])}")
            await asyncio.sleep(1)
        grade = grade_company(jds)
        return {"company": company["name"], "careers_url": company["url"],
                "grade": grade, "jds": jds}
    finally:
        try: await page.close()
        except Exception: pass


HTML_TEMPLATE = """<title>Visa-Sponsorship JD Verification</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
          background: #fafafa; margin: 0; padding: 24px 16px 60px; color: #1a1a1a; }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 22px; }}
  .legend {{ background: #fff; border: 1px solid #e6e6e6; border-radius: 10px;
             padding: 12px 14px; font-size: 13px; margin-bottom: 22px;
             display: flex; gap: 22px; flex-wrap: wrap; }}
  .pill {{ font-size: 11px; padding: 2px 9px; border-radius: 10px; font-weight: 600; }}
  .pill.GREEN {{ background: #d4f4dd; color: #1f7a3a; }}
  .pill.YELLOW {{ background: #fff4cc; color: #8a5a00; }}
  .pill.RED {{ background: #fdd; color: #a02020; }}
  .pill.UNKNOWN {{ background: #eef; color: #555; }}
  .card {{ background: #fff; border: 1px solid #e6e6e6; border-radius: 10px;
           padding: 14px 16px; margin-bottom: 14px; }}
  .head {{ display: flex; justify-content: space-between; align-items: flex-start;
           gap: 12px; margin-bottom: 8px; }}
  .company {{ font-size: 16px; font-weight: 600; }}
  .careers {{ font-size: 12px; color: #1a73e8; text-decoration: none; word-break: break-all; }}
  .careers:hover {{ text-decoration: underline; }}
  .jd {{ margin: 10px 0; padding: 8px 10px; background: #f6f8fa;
         border-radius: 6px; border-left: 3px solid #d1d5db; font-size: 12.5px; }}
  .jd.has-pos {{ border-left-color: #1f7a3a; }}
  .jd.has-neg {{ border-left-color: #a02020; }}
  .jd .title {{ font-weight: 600; margin-bottom: 4px; }}
  .jd a {{ color: #1a73e8; text-decoration: none; font-size: 11px; word-break: break-all; }}
  .quote {{ background: #fff; border: 1px solid #d4f4dd; border-left: 3px solid #1f7a3a;
            padding: 6px 9px; margin: 4px 0; font-size: 12px; line-height: 1.5;
            font-style: italic; color: #295636; border-radius: 4px; }}
  .quote.neg {{ border-color: #fdd; border-left-color: #a02020; color: #6a1c1c; }}
  .none {{ font-size: 12px; color: #888; font-style: italic; }}
</style>
<div class="container">
  <h1>Visa-Sponsorship JD Verification</h1>
  <div class="subtitle">{stamp} · companies: {n_companies} · JDs opened: {n_jds}</div>
  <div class="legend">
    <span><span class="pill GREEN">GREEN</span> &nbsp;≥2 JDs contain explicit sponsorship phrase</span>
    <span><span class="pill YELLOW">YELLOW</span> &nbsp;1 JD has it OR mixed signals</span>
    <span><span class="pill RED">RED</span> &nbsp;0 JDs surfaced explicit sponsorship</span>
    <span><span class="pill UNKNOWN">UNKNOWN</span> &nbsp;couldn't load JDs</span>
  </div>
  {cards}
</div>
"""


def _company_card(result: dict) -> str:
    name = result["company"]
    grade = result["grade"]
    careers_url = result.get("careers_url", "")
    jds = result.get("jds", [])
    pieces = [f'<div class="card"><div class="head">'
              f'<div><div class="company">{name}</div>'
              f'<a class="careers" href="{careers_url}" target="_blank">{careers_url}</a></div>'
              f'<span class="pill {grade}">{grade}</span></div>']
    if result.get("reason"):
        pieces.append(f'<div class="none">{result["reason"]}</div>')
    for j in jds:
        klass = ""
        if j.get("pos"): klass += " has-pos"
        if j.get("neg"): klass += " has-neg"
        title = (j.get("title") or j.get("anchor_text") or "JD")[:140]
        pieces.append(f'<div class="jd{klass}">')
        pieces.append(f'<div class="title">{title}</div>')
        pieces.append(f'<a href="{j["url"]}" target="_blank">{j["url"]}</a>')
        for q in j.get("pos", []):
            pieces.append(f'<div class="quote">"{q}"</div>')
        for q in j.get("neg", []):
            pieces.append(f'<div class="quote neg">⚠ "{q}"</div>')
        if not j.get("pos") and not j.get("neg"):
            pieces.append('<div class="none">no sponsorship phrases found in JD text</div>')
        pieces.append('</div>')
    pieces.append('</div>')
    return "".join(pieces)


async def main():
    print("=" * 70)
    print("VISA-SPONSORSHIP JD VERIFICATION")
    print(f"  Companies: {len(COMPANIES)} · max JDs/company: {MAX_JDS_PER_COMPANY}")
    print("=" * 70)
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        for company in COMPANIES:
            try:
                r = await verify_one(ctx, company)
            except Exception as e:
                r = {"company": company["name"], "careers_url": company["url"],
                     "grade": "UNKNOWN", "reason": f"exception:{str(e)[:80]}", "jds": []}
            results.append(r)
        await browser.close()

    # Save JSON
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"verification_{stamp}.json"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # Build HTML
    n_jds = sum(len(r.get("jds", [])) for r in results)
    cards = "".join(_company_card(r) for r in results)
    html = HTML_TEMPLATE.format(
        stamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        n_companies=len(results),
        n_jds=n_jds,
        cards=cards,
    )
    html_path = OUT_DIR / f"verification_{stamp}.html"
    html_path.write_text(html, encoding="utf-8")

    # Summary to stdout
    print(f"\n{'='*70}")
    grades = {}
    for r in results:
        g = r["grade"]
        grades[g] = grades.get(g, 0) + 1
        n_pos = sum(len(j.get("pos", [])) for j in r.get("jds", []))
        n_neg = sum(len(j.get("neg", [])) for j in r.get("jds", []))
        print(f"  [{g}] {r['company']:24s}  pos_quotes={n_pos}  neg_quotes={n_neg}")
    print(f"\n  Grades: {grades}")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
