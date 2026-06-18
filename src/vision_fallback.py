"""Claude vision fallback for Naukri chatbot questions that DOM handlers can't solve.

Used by runners/apply_naukri_kritika_chatbot.py when:
  - Radio click finds no matching candidate
  - Text input locator times out (custom dropdown rendered as text input)
  - Question is too unusual to map deterministically

Sends a screenshot of the chatbot panel + Kritika's profile facts to Claude,
gets back a CLICK/TYPE/SKIP decision, hands back to Playwright to execute.

Defaults to Haiku ($0.001/call) for cost; can opt into Opus per-call.
Includes an in-memory cache so the same question doesn't re-query Claude.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Literal, TypedDict

logger = logging.getLogger("vision-fallback")

try:
    from anthropic import Anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

# Where vision screenshots are saved for Claude CLI to read
_VISION_SHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "vision_shots"
_VISION_SHOTS_DIR.mkdir(parents=True, exist_ok=True)


# NOTE: Profile constants below intentionally omit personal contact details
# (email, phone). The vision fallback only needs role/experience/location
# context to decide what to click — never to type contact info. Email/phone
# answers come from data/form_qa.yaml + data/profile.yaml (both gitignored).
# Keep these constants free of PII so the repo stays publishable.

KRITIKA_PROFILE = """\
Profile of the applicant (candidate A):
- Role: Senior AI Engineer, 7 years experience
- Current CTC band: middle
- Notice period: 2 months / 60 days
- Current location: Bengaluru, Karnataka, India
- Willing to relocate within India: YES
- Gender: Female
- Highest qualification: Postgraduate (Master's)
- Authorized to work in India: YES
- Open to: full-time, contract, hybrid, remote
- Skills: Python, Machine Learning, GenAI, LLM, LangChain, LangGraph, RAG, AWS, Docker, FastAPI
"""

SACHIN_PROFILE = """\
Profile of the applicant (candidate B):
- Role: Senior ML Engineer, 8 years experience
- Current CTC band: upper-middle
- Expected: international roles (Berlin / Dubai / Tokyo) — visa sponsorship REQUIRED
- Notice period: 2 months / 60 days
- Current location: Bangalore, India
- Willing to relocate internationally with visa sponsorship: YES
- Authorized to work without sponsorship outside India: NO
- Gender: Male
- Highest qualification: Postgraduate
- Open to: full-time, contract, freelance, remote, hybrid
- Skills: Python, Machine Learning, GenAI/LLM, LangGraph, MCP, RAG, vector DBs
  (Weaviate, Qdrant), Claude/GPT/Llama, AWS, Azure, MLOps, real-time inference at scale
"""


class VisionDecision(TypedDict):
    action: Literal["click_text", "type_text", "skip"]
    value: str
    reasoning: str


_DECISION_CACHE: dict[str, VisionDecision] = {}


def _has_api_key() -> bool:
    return _HAS_ANTHROPIC and bool(os.getenv("ANTHROPIC_API_KEY"))


def _has_claude_cli() -> bool:
    return (shutil.which("claude") or shutil.which("claude.cmd")) is not None


def _shell_quote(s: str) -> str:
    """Wrap a prompt string for cmd.exe — escape doublequotes by doubling them."""
    return '"' + s.replace('"', '""') + '"'


def _build_prompt(question: str, profile: str = None) -> str:
    """Build the Claude prompt. `profile` MUST be passed by caller — defaults to
    Kritika's profile only for legacy compatibility. New code must pass it explicitly.
    """
    if profile is None:
        profile = KRITIKA_PROFILE  # legacy default
    return f"""You're looking at the right-side chatbot panel of a Naukri.com job application form.

Current question: "{question}"

{profile}

Look at the screenshot. Decide what the applicant should do.

Respond with EXACTLY this format (three lines):
ACTION: <CLICK|TYPE|SKIP>
VALUE: <text>
REASONING: <one sentence>

Where ACTION is one of:
  CLICK  — radio buttons / dropdown options are visible. VALUE = EXACT on-screen
           text of the option to click. Match case exactly. Pick the option that
           best fits the applicant's profile. NEVER pick "No experience",
           "Less than 3", "0 years", or any option that would disqualify them.
  TYPE   — a text input box is visible. VALUE = short factual answer to type.
  SKIP   — question is risky/ambiguous/could submit a wrong answer. VALUE = empty.

Examples (use the applicant's actual profile, not these literal answers):
ACTION: CLICK
VALUE: 5-7 years
REASONING: Profile shows 7 years experience, fits the 5-7 band.

ACTION: TYPE
VALUE: <applicant's current employer name>
REASONING: Profile lists this as the current employer.

ACTION: SKIP
VALUE:
REASONING: Question asks about certifications the applicant may not hold.
"""


async def vision_decide(
    screenshot_bytes: bytes,
    question: str,
    model: str = "claude-haiku-4-5-20251001",
    profile: str = None,
) -> VisionDecision | None:
    """Get a CLICK/TYPE/SKIP decision from Claude.

    Two backends, tried in order:
      1. Anthropic SDK (if ANTHROPIC_API_KEY is set) — fastest, batched API call
      2. Claude CLI subprocess (`claude -p`) — uses OAuth login, no API key needed
    """
    if question in _DECISION_CACHE:
        logger.info(f"  [vision] cache hit: {question[:60]}")
        return _DECISION_CACHE[question]

    decision: VisionDecision | None = None
    if _has_api_key():
        decision = await _decide_via_sdk(screenshot_bytes, question, model, profile)
    if decision is None and _has_claude_cli():
        decision = await _decide_via_cli(screenshot_bytes, question, profile)

    if decision is None:
        logger.warning("  [vision] no backend available — set ANTHROPIC_API_KEY or install claude CLI")
        return None

    logger.info(f"  [vision] {decision['action']} → {decision['value'][:60]}  ({decision['reasoning'][:80]})")
    _DECISION_CACHE[question] = decision
    return decision


async def _decide_via_sdk(
    screenshot_bytes: bytes,
    question: str,
    model: str,
    profile: str = None,
) -> VisionDecision | None:
    """Direct Anthropic SDK call with inline image."""
    client = Anthropic()
    img_b64 = base64.standard_b64encode(screenshot_bytes).decode("ascii")
    try:
        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": img_b64,
                    }},
                    {"type": "text", "text": _build_prompt(question, profile)},
                ],
            }],
        )
    except Exception as e:
        logger.warning(f"  [vision-sdk] API error: {str(e)[:120]}")
        return None
    text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    return _parse_response(text)


async def _decide_via_cli(
    screenshot_bytes: bytes,
    question: str,
    profile: str = None,
) -> VisionDecision | None:
    """Spawn `claude -p` subprocess and let Claude Code Read the screenshot.

    Uses your existing OAuth login — no API key required.
    """
    # Save screenshot to disk so claude can Read it
    ts = int(time.time() * 1000)
    img_path = _VISION_SHOTS_DIR / f"q_{ts}.png"
    img_path.write_bytes(screenshot_bytes)

    prompt = (
        f"Read the screenshot at {img_path}. "
        + _build_prompt(question, profile)
        + "\n\nIMPORTANT: Output ONLY the three lines (ACTION/VALUE/REASONING). "
        + "No preamble, no markdown, no code fence. Just the three lines."
    )

    # Windows: pipe prompt via stdin to avoid shell-escaping headaches with the .cmd shim
    claude_path = shutil.which("claude.cmd") or shutil.which("claude") or "claude"
    try:
        if os.name == "nt":
            cmd = (
                f'"{claude_path}" -p --output-format text '
                f'--allowedTools Read --dangerously-skip-permissions '
                f'--add-dir "{_VISION_SHOTS_DIR}"'
            )
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                claude_path, "-p", "--output-format", "text",
                "--allowedTools", "Read",
                "--dangerously-skip-permissions",
                "--add-dir", str(_VISION_SHOTS_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=120,
        )
    except asyncio.TimeoutError:
        logger.warning("  [vision-cli] timed out after 90s")
        return None
    except Exception as e:
        logger.warning(f"  [vision-cli] subprocess error: {str(e)[:120]}")
        return None

    if proc.returncode != 0:
        logger.warning(f"  [vision-cli] exit {proc.returncode}: {stderr.decode(errors='replace')[:200]}")
        return None

    text = stdout.decode(errors="replace").strip()
    return _parse_response(text)


def _parse_response(text: str) -> VisionDecision:
    """Parse 'ACTION: X\\nVALUE: Y\\nREASONING: Z' format. Tolerant to whitespace + ordering."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    action = "skip"
    value = ""
    reasoning = ""

    for line in lines:
        upper = line.upper()
        if upper.startswith("ACTION:"):
            raw = line.split(":", 1)[1].strip().upper()
            if "CLICK" in raw:
                action = "click_text"
            elif "TYPE" in raw:
                action = "type_text"
            else:
                action = "skip"
        elif upper.startswith("VALUE:"):
            value = line.split(":", 1)[1].strip()
        elif upper.startswith("REASONING:"):
            reasoning = line.split(":", 1)[1].strip()

    return {"action": action, "value": value, "reasoning": reasoning}
