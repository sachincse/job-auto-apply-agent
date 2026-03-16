---
name: job-search
description: Search for jobs across multiple platforms (Adzuna, Remotive, LinkedIn, Indeed) based on user profile and preferences
trigger: When user asks to search or find jobs, or when scheduler triggers a search cycle
---

# Job Search Skill

## What it does
Searches for relevant job openings across multiple job platforms using APIs and browser automation, then scores each result for fit using Claude AI.

## Workflow

1. **Load profile** — Read `data/profile.yaml` for target roles, skills, location, salary range
2. **Search APIs** — Query each enabled platform:
   - **Adzuna API** — `GET /api/1/search` with keywords, location, salary filters
   - **Remotive API** — `GET /api/remote-jobs` filtered by category and tags
   - **LinkedIn** — Use Playwright to search `linkedin.com/jobs/search` with filters
   - **Indeed** — Use Playwright to search `indeed.com/jobs` with query params
3. **Deduplicate** — Check `data/jobs.db` → `searched_jobs` table to skip already-seen listings
4. **AI Scoring** — Send job title + description to Claude API, get a 0-100 fit score based on profile
5. **Store results** — Insert into `searched_jobs` table with score, source, URL, timestamp
6. **Return top matches** — Return jobs scoring above threshold (default: 70)

## Configuration (profile.yaml)
```yaml
job_search:
  keywords: ["software engineer", "backend developer", "python developer"]
  location: "Remote"  # or "New York, NY"
  salary_min: 80000
  experience_level: "mid"  # junior, mid, senior
  job_type: "full-time"  # full-time, contract, part-time
  platforms:
    adzuna: true
    remotive: true
    linkedin: true
    indeed: true
  score_threshold: 70
  max_results_per_platform: 25
```

## Database Schema
```sql
CREATE TABLE searched_jobs (
    id INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    source TEXT,
    salary_min REAL,
    salary_max REAL,
    description TEXT,
    fit_score INTEGER,
    status TEXT DEFAULT 'new',  -- new, applied, skipped, rejected
    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Error Handling
- If an API is down or rate-limited, skip it and log warning
- If LinkedIn blocks the session, pause LinkedIn searches for 1 hour
- Always return partial results rather than failing entirely
