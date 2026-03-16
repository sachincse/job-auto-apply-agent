"""Claude API integration for AI-powered decisions."""

import anthropic
from src.config import ANTHROPIC_API_KEY, load_profile


client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _profile_summary() -> str:
    profile = load_profile()
    p = profile["personal"]
    s = profile["skills"]
    e = profile["experience"]
    return (
        f"Name: {p['name']}\n"
        f"Skills: {', '.join(s['primary'] + s['frameworks'] + s['tools'])}\n"
        f"Experience: {e['years']} years, {e['level']} level\n"
        f"Summary: {e['summary']}\n"
        f"Looking for: {', '.join(profile['job_search']['keywords'])}\n"
        f"Location pref: {profile['job_search']['location']}\n"
        f"Salary range: ${profile['job_search'].get('salary_min', 'N/A')}-${profile['job_search'].get('salary_max', 'N/A')}"
    )


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
