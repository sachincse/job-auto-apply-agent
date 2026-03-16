# Job Auto-Apply Agent

## Project Overview
An AI-powered job hunting automation system that searches, applies, responds, and reports — running on a schedule.

## Architecture

```
job-agent/
├── CLAUDE.md              # This file — project instructions
├── skills/                # Claude Code skill definitions
│   ├── job-search.md      # Search jobs across platforms
│   ├── auto-apply.md      # Auto-apply to matched jobs
│   ├── linkedin-engage.md # LinkedIn post search & engagement
│   ├── auto-respond.md    # Auto-respond to recruiter messages
│   ├── report.md          # Send reports via email/Telegram
│   └── scheduler.md       # Orchestrate scheduled runs
├── src/
│   ├── config.py          # Central configuration loader
│   ├── job_searcher.py    # Job search across APIs (Indeed, Adzuna, Remotive, LinkedIn)
│   ├── applicant.py       # Auto-fill & submit applications
│   ├── linkedin_scanner.py# Scan LinkedIn posts for hiring signals
│   ├── messenger.py       # Auto-draft replies to recruiter messages
│   ├── reporter.py        # Email & Telegram reporting
│   ├── scheduler.py       # APScheduler-based cron runner
│   ├── browser.py         # Playwright browser automation helpers
│   ├── ai_engine.py       # Claude API integration for smart decisions
│   └── db.py              # SQLite tracking (applied jobs, messages, logs)
├── templates/
│   ├── cover_letter.jinja # Cover letter template
│   ├── reply_message.jinja# Message reply template
│   └── report.jinja       # Daily report template
├── data/
│   ├── resume.pdf         # User's resume (place here)
│   ├── profile.yaml       # User profile, skills, preferences
│   └── jobs.db            # SQLite database (auto-created)
├── .env.example           # Environment variable template
├── requirements.txt       # Python dependencies
└── main.py                # Entry point
```

## Key Design Decisions

- **API-first approach**: Use official APIs (Adzuna, Remotive, LinkedIn API) where available. Fall back to Playwright browser automation only when no API exists.
- **LinkedIn caution**: LinkedIn aggressively bans bots. The LinkedIn module uses the official API for reading (requires LinkedIn Developer app) and Playwright with human-like delays for actions. User must accept the risk.
- **Semi-automated messaging**: AI drafts message replies; can be set to auto-send or require approval.
- **SQLite tracking**: Every job searched, applied, and message sent is logged in `data/jobs.db` to avoid duplicates and enable reporting.
- **Claude AI brain**: Uses Claude API to score job fit, generate cover letters, draft message replies, and decide whether to apply.

## Tech Stack
- Python 3.11+
- Playwright (browser automation)
- APScheduler (cron scheduling)
- Claude API via `anthropic` SDK (AI decisions)
- Jinja2 (templating)
- SQLite (local database)
- python-telegram-bot (Telegram reports)
- smtplib (email reports)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Setup
cp .env.example .env  # Fill in API keys
# Edit data/profile.yaml with your details

# Run once (all steps)
python main.py --run-once

# Run scheduler (continuous)
python main.py --schedule

# Run specific module
python main.py --search-only
python main.py --apply-only
python main.py --report-only
```

## Environment Variables (see .env.example)
- `ANTHROPIC_API_KEY` — Claude API key for AI decisions
- `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` — Adzuna job search API
- `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` — LinkedIn credentials
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Telegram reporting
- `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `REPORT_EMAIL` — Email reporting

## Rules for Claude Code
1. Never hardcode credentials — always read from `.env`
2. Never commit `.env` or `data/jobs.db`
3. Always add human-like random delays (2-8s) in browser automation
4. Log every action to SQLite before executing it
5. Respect rate limits on all APIs
6. When modifying browser automation, test with `--headless=false` first
7. Cover letters and messages must sound human, not robotic
8. Always deduplicate — check DB before applying to same job twice
9. Use type hints and keep functions small and testable
