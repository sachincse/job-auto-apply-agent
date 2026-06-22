"""Claude API integration for AI-powered decisions."""

import anthropic
from src.config import ANTHROPIC_API_KEY, load_profile


client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _profile_summary() -> str:
    """Schema-defensive: tolerate missing keys (profile.yaml shape varies)."""
    profile = load_profile() or {}
    p = profile.get("personal", {}) or {}
    s = profile.get("skills", {}) or {}
    e = profile.get("experience", {}) or {}
    js = profile.get("job_search", {}) or {}

    skills_list = (s.get("primary", []) or []) + (s.get("frameworks", []) or []) + (s.get("tools", []) or [])

    # Location: try 'location', then 'location_targets' (list of {name,...}), then fall back
    loc = js.get("location")
    if not loc:
        targets = js.get("location_targets") or []
        if targets:
            loc = ", ".join(t.get("name", "") for t in targets if isinstance(t, dict) and t.get("name"))
    loc = loc or "Remote / Worldwide"

    summary = e.get("summary") or e.get("current_employer") or "Senior engineer"
    keywords = js.get("keywords", []) or []

    return (
        f"Name: {p.get('name', 'Candidate')}\n"
        f"Skills: {', '.join(skills_list) if skills_list else 'Python, ML, AI'}\n"
        f"Experience: {e.get('years', 'N/A')} years, {e.get('level', 'senior')} level\n"
        f"Summary: {summary}\n"
        f"Looking for: {', '.join(keywords) if keywords else 'AI/ML engineering roles'}\n"
        f"Location pref: {loc}\n"
        f"Salary range: ${js.get('salary_min', 'N/A')}-${js.get('salary_max', 'N/A')}"
    )


def _keyword_score(title: str, company: str, description: str) -> int:
    """Fast offline scorer — no LLM call. Used when iterating through many jobs.
    Returns 0-100 based on simple keyword matches against AI/ML/senior signals.
    """
    text = f"{title} {company} {description}".lower()
    score = 0
    # Strong positive signals
    for kw, weight in [
        ("senior", 12), ("lead", 12), ("staff", 10), ("principal", 10),
        ("machine learning", 15), ("ml engineer", 15), ("ai engineer", 15),
        ("generative ai", 12), ("llm", 10), ("genai", 10),
        ("langchain", 8), ("langgraph", 10), ("rag", 8),
        ("computer vision", 6), ("nlp", 6), ("deep learning", 6),
        ("mlops", 8), ("data scientist", 8),
        ("python", 5), ("aws", 4), ("gcp", 4), ("azure", 4),
        ("visa", 8), ("sponsorship", 10), ("relocation", 8), ("blue card", 12),
        ("remote", 5), ("hybrid", 3),
        ("dubai", 6), ("berlin", 6), ("tokyo", 6), ("amsterdam", 6),
        ("singapore", 4), ("munich", 6),
    ]:
        if kw in text:
            score += weight
    # Mild negative for junior/intern
    for kw, weight in [("intern", -20), ("junior", -10), ("entry level", -15)]:
        if kw in text:
            score += weight
    return max(0, min(100, score))


def score_job(title: str, company: str, description: str) -> int:
    """Score a job 0-100 for how well it matches the user's profile."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                f"Score this job 0-100 for fit with this candidate. "
                f"Reply with ONLY a number.\n\n"
                f"CANDIDATE:\n{_profile_summary()}\n\n"
                f"JOB:\nTitle: {title}\nCompany: {company}\n"
                f"Description: {description[:2000]}"
            ),
        }],
    )
    try:
        return int(response.content[0].text.strip())
    except ValueError:
        return 50


def generate_cover_letter(job: dict) -> str:
    """Generate a tailored cover letter for a specific job."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": (
                f"Write a concise, professional cover letter (3 paragraphs max) "
                f"for this job. Sound human, not robotic. No fluff.\n\n"
                f"CANDIDATE:\n{_profile_summary()}\n\n"
                f"JOB:\nTitle: {job['title']}\nCompany: {job['company']}\n"
                f"Description: {job.get('description', 'N/A')[:2000]}"
            ),
        }],
    )
    return response.content[0].text


def classify_message(message: str) -> dict:
    """Classify a recruiter message and draft a reply."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this message and draft a reply.\n\n"
                f"CANDIDATE PROFILE:\n{_profile_summary()}\n\n"
                f"MESSAGE:\n{message[:2000]}\n\n"
                f"Respond in this exact format:\n"
                f"CLASSIFICATION: <job_opportunity|interview_request|rejection|follow_up|spam|other>\n"
                f"FIT_SCORE: <0-100>\n"
                f"DRAFT_REPLY:\n<your drafted reply>"
            ),
        }],
    )
    text = response.content[0].text
    result = {"classification": "other", "fit_score": 50, "draft_reply": ""}
    for line in text.split("\n"):
        if line.startswith("CLASSIFICATION:"):
            result["classification"] = line.split(":", 1)[1].strip().lower()
        elif line.startswith("FIT_SCORE:"):
            try:
                result["fit_score"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("DRAFT_REPLY:"):
            result["draft_reply"] = text.split("DRAFT_REPLY:", 1)[1].strip()
            break
    return result


def score_linkedin_post(post_content: str) -> int:
    """Score a LinkedIn hiring post for relevance."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": (
                f"Score 0-100 how relevant this hiring post is for this candidate. "
                f"Reply with ONLY a number.\n\n"
                f"CANDIDATE:\n{_profile_summary()}\n\n"
                f"POST:\n{post_content[:1500]}"
            ),
        }],
    )
    try:
        return int(response.content[0].text.strip())
    except ValueError:
        return 50


def generate_comment(post_content: str) -> str:
    """Generate a professional comment for a LinkedIn hiring post."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Write a short (2-3 sentences), professional LinkedIn comment "
                f"expressing interest in this hiring post. Reference something "
                f"specific from the post. Don't be generic or spammy.\n\n"
                f"CANDIDATE:\n{_profile_summary()}\n\n"
                f"POST:\n{post_content[:1500]}"
            ),
        }],
    )
    return response.content[0].text
