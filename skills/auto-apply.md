---
name: auto-apply
description: Automatically apply to jobs that match the user's profile above the score threshold
trigger: When user asks to apply to jobs, or when scheduler triggers after job-search completes
---

# Auto-Apply Skill

## What it does
Takes high-scoring jobs from the search results and automatically submits applications using browser automation and AI-generated cover letters.

## Workflow

1. **Fetch candidates** — Query `searched_jobs` where `fit_score >= threshold` AND `status = 'new'`
2. **For each job**:
   a. **Generate cover letter** — Use Claude API with job description + profile to create a tailored cover letter from `templates/cover_letter.jinja`
   b. **Open application page** — Playwright navigates to the job URL
   c. **Detect application type**:
      - **Easy Apply (LinkedIn)** — Fill modal fields, attach resume, submit
      - **External ATS (Greenhouse, Lever, Workday)** — Fill standard form fields
      - **Email apply** — Compose and send email with resume + cover letter
   d. **Fill form fields** — Map profile data to form fields using AI field detection
   e. **Attach resume** — Upload `data/resume.pdf`
   f. **Submit** — Click submit button
   g. **Update DB** — Set `status = 'applied'`, store cover letter used, timestamp
3. **Handle failures** — If form can't be filled or submitted, set `status = 'failed'` with error reason

## Supported Platforms
| Platform | Method | Notes |
|----------|--------|-------|
| LinkedIn Easy Apply | Playwright | Human-like delays, handle multi-step modals |
| Greenhouse | Playwright | Standard form detection |
| Lever | Playwright | Standard form detection |
| Workday | Playwright | Complex — may need per-company config |
| Email applications | SMTP | Send formatted email with attachments |
| Company websites | Playwright | Best-effort AI form filling |

## AI Form Filling Strategy
```
For each form field:
1. Read field label, placeholder, name attribute
2. Match to profile data (name, email, phone, LinkedIn URL, etc.)
3. For free-text fields (e.g., "Why do you want to work here?"), use Claude to generate a response
4. For dropdowns, use fuzzy matching to select the best option
5. For checkboxes (e.g., "I agree to terms"), check them
```

## Safety
- **Max 20 applications per day** to avoid looking like a bot
- **Random delays** between 3-10 seconds per action
- **Screenshot before submit** — saved to `data/screenshots/` for audit
- **Dry-run mode** — `--dry-run` fills forms but doesn't click submit
- **Never apply to the same job twice** — enforced by DB unique constraint

## Database Schema Addition
```sql
CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES searched_jobs(id),
    cover_letter TEXT,
    screenshot_path TEXT,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'submitted',  -- submitted, confirmed, rejected, interview
    response TEXT
);
```
