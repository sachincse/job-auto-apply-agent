"""HR/recruiter connect-note generator for Sachin's post-apply outreach.

Templates designed by the hr-connect-note-design workflow (2026-06-13):
adversarial review across recruiter perspective + anti-spam + conversion lens.

Public API:
    generate_company_context(company, jd_excerpt) -> str  # 4-8 word phrase via claude -p
    pick_variant(role_title, company_context) -> str       # 'default' | 'v1_outcome' | 'v2_question'
    render_note(profile, variant, ...) -> str              # ≤300 chars guaranteed
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger("hr-connect")

# Three templates, all designed to fit 300 chars after substitution.
# {first_name}≤12, {role}≤32, {company}≤24, {company_context}≤32.
TEMPLATES = {
    "default": (
        "Hi {first_name} — 8yr senior ML, just applied for {role} at {company}. "
        "Saw you're building {company_context} — same space I ship in: LangGraph + RAG "
        "(Weaviate/Qdrant, Claude/GPT) in regulated fintech at TrueBalance, "
        "sub-200ms inference at 10M+ users. Worth a quick chat?"
    ),
    "v1_outcome": (
        "Hi {first_name}, shipped sub-200ms RAG + multi-agent LLM stack at TrueBalance "
        "serving 10M+ lending users under RBI rules. Saw {company}'s work on "
        "{company_context} — same problem space — and applied for {role}. "
        "8yr senior ML; happy to walk through the eval + guardrails setup."
    ),
    "v2_question": (
        "Hi {first_name}, curious how your team handles agent eval + reliability as "
        "{company_context} scales? Just applied for {role} at {company}. "
        "8yr senior ML at TrueBalance — LangGraph + RAG (Weaviate/Qdrant) under RBI, "
        "sub-200ms at 10M+ users. Would value swapping notes."
    ),
    # Pre-apply outreach: candidate hasn't formally applied yet, just expressing
    # interest. Used for high-value targets like Google India where you want
    # recruiter ack BEFORE submitting through the portal.
    "v3_interest": (
        "Hi {first_name} — 8yr senior ML at TrueBalance (regulated fintech, "
        "sub-200ms RAG + multi-agent LLM at 10M+ users). Targeting {role} at "
        "{company} — {company_context} lines up with my stack. Would value 15 "
        "min on whether my background fits your team's hiring."
    ),
}

# Trim sequence applied in order until ≤300. Workflow design notes (b).
TRIM_SEQUENCE = [
    ("(Weaviate/Qdrant, Claude/GPT)", "(Weaviate+Claude)"),
    ("(Weaviate/Qdrant)", "(Weaviate)"),
    ("sub-200ms inference at 10M+ users", "sub-200ms at scale"),
    ("at 10M+ lending users under RBI rules", "at scale under RBI"),
    ("happy to walk through the eval + guardrails setup.", "open to chat."),
    ("Would value swapping notes.", "Open to connect."),
    (" Worth a quick chat?", ""),
    (" — same problem space —", " —"),
    (" — same space I ship in:", " — same space:"),
]

_VISION_SHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "vision_shots"
_VISION_SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# In-process cache: company_name (normalized) -> company_context phrase
_CONTEXT_CACHE: dict[str, str] = {}


def _has_claude_cli() -> bool:
    return (shutil.which("claude") or shutil.which("claude.cmd")) is not None


def _normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


async def generate_company_context(company: str, jd_excerpt: str = "") -> str:
    """Returns a 4-8 word phrase describing what the company builds/solves.
    Cache-hit returns instantly. Uses `claude -p` (no API key needed).
    Falls back to generic phrase if LLM unavailable or invalid response.
    """
    key = _normalize_company(company)
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]

    fallback = "AI/ML systems in production"

    if not _has_claude_cli():
        logger.warning("[hr-connect] claude CLI missing — using fallback context")
        _CONTEXT_CACHE[key] = fallback
        return fallback

    jd_excerpt = (jd_excerpt or "").strip()[:1500]
    prompt = (
        f"From the company name and job description below, output ONLY a 4-8 word phrase "
        f"describing what this team is actively building or solving "
        f'(e.g., "agentic AI for fintech compliance", "real-time fraud ML at scale"). '
        f'No fluff, no adjectives like "innovative", no "the team", no quotes, no trailing punctuation. '
        f"If unclear, output the product area in 4-6 words.\n\n"
        f"Company: {company}\n"
        f"JD: {jd_excerpt or '(no JD; use general product area for this company)'}"
    )
    claude_path = shutil.which("claude.cmd") or shutil.which("claude") or "claude"
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
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=90,
        )
    except asyncio.TimeoutError:
        logger.warning("[hr-connect] claude CLI timed out — using fallback")
        _CONTEXT_CACHE[key] = fallback
        return fallback
    except Exception as e:
        logger.warning(f"[hr-connect] claude CLI error: {str(e)[:80]}")
        _CONTEXT_CACHE[key] = fallback
        return fallback

    text = stdout.decode("utf-8", errors="replace").strip()
    # Sanitize: strip quotes, trailing punctuation
    text = text.strip().strip('"“”\'`').rstrip(".,!?;: ")
    # Drop everything after first newline
    text = text.split("\n")[0].strip()
    # Validate: 3-10 words, ≤60 chars (templates designed to absorb up to ~50 cleanly)
    word_count = len(text.split())
    if not (3 <= word_count <= 10) or len(text) > 60 or len(text) < 8:
        logger.info(f"[hr-connect] CLI returned invalid context ({text!r}) — fallback")
        _CONTEXT_CACHE[key] = fallback
        return fallback

    _CONTEXT_CACHE[key] = text
    return text


def pick_variant(role_title: str, company_context: str) -> str:
    """Variant selection per workflow design notes (c):
      - v1_outcome for HMs / eng leads
      - v2_question for agent/eval/LLM-heavy contexts
      - default for everyone else (recruiters/HR)
    """
    r = (role_title or "").lower()
    c = (company_context or "").lower()
    if any(k in r for k in ("engineer", "lead", "principal", "cto", "head of", "vp", "director")):
        return "v1_outcome"
    if any(k in c for k in ("agent", "eval", " llm")):
        return "v2_question"
    return "default"


def _trim_to_fit(text: str, limit: int = 300) -> str:
    """Apply ordered trim sequence until ≤limit. Word-boundary safe at every step."""
    out = text
    if len(out) <= limit:
        return out
    for old, new in TRIM_SEQUENCE:
        if old in out:
            out = out.replace(old, new)
            if len(out) <= limit:
                return out
    # Last resort: word-boundary truncation
    if len(out) > limit:
        cut = out[: limit - 1]
        sp = cut.rfind(" ")
        if sp > limit - 40:
            cut = cut[:sp]
        out = cut.rstrip(" ,.;:") + "…"
    return out


def _word_trim(s: str, max_chars: int) -> str:
    """Truncate at last whole word that fits — never mid-word."""
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    # Drop trailing partial word
    sp = cut.rfind(" ")
    return cut[:sp].rstrip(" ,.;:") if sp > 0 else cut


def render_note(
    first_name: str,
    role: str,
    company: str,
    company_context: str,
    variant: str = "default",
) -> str:
    """Render and trim to 300 chars. All field caps trim at word boundary."""
    fn = (first_name or "there").strip().split()[0][:14]
    role = _word_trim(role or "the role", 32)
    company = _word_trim(company or "your team", 24)
    cc = _word_trim(company_context or "AI/ML systems in production", 60)
    tpl = TEMPLATES.get(variant, TEMPLATES["default"])
    rendered = tpl.format(first_name=fn, role=role, company=company, company_context=cc)
    return _trim_to_fit(rendered, 300)


async def build_note_for(
    first_name: str,
    role: str,
    company: str,
    jd_excerpt: str = "",
) -> tuple[str, str, str]:
    """High-level: returns (rendered_note, variant, company_context)."""
    cc = await generate_company_context(company, jd_excerpt)
    variant = pick_variant(role, cc)
    note = render_note(first_name, role, company, cc, variant)
    return note, variant, cc
