# Job Auto-Apply Agent

An AI-powered job-search automation toolkit. Searches multiple platforms, auto-applies to matched roles, drafts capability-matched recruiter outreach, and reports daily — designed to keep two parallel candidate pipelines running on autopilot.

> **Note**: this is a personal automation project. Use at your own risk. Some sites (LinkedIn especially) actively rate-limit and ban automated activity.

---

## What it does

| Pipeline | Tech | What it produces |
|---|---|---|
| **Naukri Quick Apply (chatbot)** | Playwright + Claude vision fallback via `claude -p` CLI | ~30-80 real applications per session, end-to-end (radio + text + dropdown questions, JD-aware skip on disqualifying options) |
| **LinkedIn Easy Apply** | Playwright + form classifier | ~2-10 Easy Apply submits per session, with post-apply hiring-manager connect requests |
| **HR / Recruiter Outreach** | Playwright + workflow-designed connect-note templates | Capability-matched LinkedIn connect notes (per-company researched context) for recruiters at companies you've applied to, or pre-apply targeted outreach (e.g. Google India recruiters) |
| **Direct-email apply** | SMTP | Cover-letter PDF + résumé to direct recruiter email addresses |
| **Daily report** | Jinja → Email + Telegram | What ran, what submitted, what failed, what to do tomorrow |

The notable architectural piece is the **hybrid Playwright + LLM vision fallback** for Naukri's chatbot: when the deterministic form classifier can't resolve a question (e.g. custom dropdowns, irregular phrasing), the runner screenshots the chatbot panel, pipes prompt+image-path to a `claude -p` subprocess, and acts on the returned `ACTION/VALUE/REASONING` triple. Per-question in-memory cache so repeats cost zero LLM calls. No API key needed — uses Claude Code's OAuth session.

---

## Repository structure

```
job-auto-apply-agent/
├── main.py                              # Entry point: --run-once, --schedule, --task <name>
├── CLAUDE.md                            # Instructions for Claude Code (form-fill rules, conventions)
├── README.md                            # This file
├── requirements.txt
├── .env.example                         # Template for credentials (copy → .env, fill in)
│
├── src/                                 # Core library
│   ├── config.py                        # .env + profile.yaml loader
│   ├── browser.py                       # Playwright + playwright-stealth helpers
│   ├── db.py                            # SQLite schema + queries
│   ├── ai_engine.py                     # Claude API or keyword-fallback for job scoring
│   ├── form_classifier.py               # Question → answer classifier (uses data/form_qa.yaml)
│   ├── vision_fallback.py               # Naukri chatbot vision fallback (claude -p subprocess)
│   ├── hr_connect.py                    # Workflow-designed connect-note templates + name blocklist
│   ├── job_searcher.py                  # Multi-platform job search (Adzuna, Remotive, LinkedIn)
│   ├── applicant.py                     # Browser-based application submitter
│   ├── reporter.py                      # Email + Telegram daily reports
│   └── scheduler.py                     # APScheduler cron task registry
│
├── runners/                             # Standalone scripts — one per task
│   ├── apply_naukri_sachin_chatbot.py   # Naukri chatbot apply (candidate A)
│   ├── apply_naukri_kritika_chatbot.py  # Naukri chatbot apply (candidate B)
│   ├── apply_linkedin.py                # LinkedIn Easy Apply (candidate A)
│   ├── apply_kritika.py                 # LinkedIn Easy Apply (candidate B)
│   ├── connect_hr_sachin.py             # Post-apply HR outreach (researched per-company notes)
│   └── connect_hr_google_india.py       # Targeted Google India recruiter outreach
│
├── data/                                # User data (most gitignored)
│   ├── profile.example.yaml             # Profile template — copy to profile.yaml, fill in
│   ├── profile.yaml                     # YOUR profile (gitignored)
│   ├── form_qa.yaml                     # Question pattern → answer map (gitignored)
│   ├── resume.pdf                       # CV (gitignored)
│   ├── jobs.db                          # SQLite (auto-created, gitignored)
│   └── cookies/                         # Saved sessions per portal (gitignored)
│
├── templates/                           # Jinja templates
│   ├── cover_letter.jinja
│   ├── reply_message.jinja
│   └── report.jinja
│
└── skills/                              # Claude Code skill definitions
```

---

## Setup

```bash
# 1. Clone and install dependencies
git clone https://github.com/sachincse/job-auto-apply-agent.git
cd job-auto-apply-agent
pip install -r requirements.txt
playwright install chromium

# 2. Configure credentials
cp .env.example .env
# Edit .env — at minimum set LINKEDIN_EMAIL / LINKEDIN_PASSWORD if you use the LinkedIn runner

# 3. Configure your profile
cp data/profile.example.yaml data/profile.yaml
# Edit data/profile.yaml — fill in name, email, phone, location, experience, skills, etc.

# 4. Place your résumé
cp /path/to/your/cv.pdf data/resume.pdf

# 5. (One-time) Log in to portals + save cookies — headed browser, you log in manually
python tools/setup/login_platforms.py
```

---

## Usage

### One-off runs

```bash
# Naukri (chatbot Q&A, vision fallback for unknown questions)
python runners/apply_naukri_sachin_chatbot.py
python runners/apply_naukri_kritika_chatbot.py

# LinkedIn Easy Apply — DRY-RUN by default; pass --live to actually submit
python runners/apply_linkedin.py --live --max=10
python runners/apply_kritika.py  --live --max=12

# HR outreach — surfaces hiring-team profiles, generates personalized notes
python runners/connect_hr_sachin.py        --max=8   # DRY-RUN default
python runners/connect_hr_sachin.py --live --max=3   # actually send 3 connects

# Targeted: Google India recruiter outreach (pre-apply outreach for high-value target)
python runners/connect_hr_google_india.py --live --max=3
```

### Scheduled run (daily cron)

```bash
python main.py --schedule
```

Runs the full pipeline on a schedule defined in `src/scheduler.py`. Sends a daily report via email + Telegram when done.

---

## Design decisions

- **API-first where possible** — Adzuna / Remotive / Arbeitnow have free APIs and don't require browser automation. LinkedIn and Naukri don't, so those use Playwright.
- **Cookies, not credentials** — runners restore from saved cookie files. Re-login only on cookie expiry. Prevents auto-detection from frequent password attempts.
- **Per-applicant isolation** — every candidate has separate cookies, screenshot dirs, results JSON, and answer banks. Two pipelines never cross-contaminate.
- **DRY-RUN by default for irreversible actions** — LinkedIn Easy Apply, HR connect requests, and direct emails all require `--live` to actually send. Preview the queue first.
- **Hard blocklist** — `src/hr_connect.is_blocked()` lets you mark names that must never be searched, viewed, or messaged. Filter is applied twice (harvest + queue) for defense-in-depth.
- **No PII in repo** — `data/profile.yaml`, `data/form_qa.yaml`, `data/cookies/`, `data/*.pdf`, and `.env` are all gitignored. Only the `.example.yaml` template is committed.

---

## Tech stack

- Python 3.11+
- Playwright + playwright-stealth (browser automation)
- APScheduler (cron scheduling)
- `anthropic` SDK + `claude -p` CLI (LLM decisions; CLI path avoids needing a paid API key)
- Jinja2 (templating)
- SQLite + aiosqlite (local DB)
- python-telegram-bot + smtplib (reporting)

---

## Caveats

- **LinkedIn actively rate-limits automation.** Stick to small daily caps (5-8 connects/day, 10-12 Easy Apply/day) and use realistic delays. Account bans are real.
- **"Add a note" on LinkedIn connects requires Premium** on most account states. Free accounts hit a `no_send_btn` failure when the runner clicks Add-a-note. Use the manual-paste workflow if hitting this.
- **Naukri Akamai blocks headless Chrome.** All Naukri runners use headed mode (`headless=False`).
- **Form filling is rule-based, not LLM-driven** for most questions. The classifier-first / vision-fallback split is intentional — LLM calls are slow and cost adds up at scale.

---

## License

Personal-use project. Forks welcome; please don't redistribute committed cookies / résumés / answer banks if you mirror this.
