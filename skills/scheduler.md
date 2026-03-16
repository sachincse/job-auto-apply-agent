---
name: scheduler
description: Orchestrate all job hunting tasks on a configurable schedule using APScheduler
trigger: When user starts the agent in scheduled mode, or asks to configure the schedule
---

# Scheduler Skill

## What it does
Runs all job hunting tasks on a configurable cron-like schedule using APScheduler. Acts as the orchestration layer that ties all other skills together.

## Default Schedule

| Time | Task | Frequency |
|------|------|-----------|
| 08:00 | Job Search (all platforms) | Daily |
| 09:00 | Auto-Apply (to overnight matches) | Daily |
| 10:00 | LinkedIn Post Scan | Every 6 hours |
| 12:00 | Check Messages & Auto-Respond | Every 2 hours |
| 16:00 | LinkedIn Post Scan | Every 6 hours |
| 20:00 | Daily Report | Daily |
| SUN 20:00 | Weekly Report | Weekly |

## Configuration
```yaml
scheduler:
  timezone: "US/Eastern"
  jobs:
    search:
      cron: "0 8 * * *"        # 8 AM daily
      enabled: true
    apply:
      cron: "0 9 * * *"        # 9 AM daily
      enabled: true
      max_per_run: 10
    linkedin_scan:
      cron: "0 */6 * * *"      # Every 6 hours
      enabled: true
    check_messages:
      cron: "0 */2 * * *"      # Every 2 hours
      enabled: true
    daily_report:
      cron: "0 20 * * *"       # 8 PM daily
      enabled: true
    weekly_report:
      cron: "0 20 * * 0"       # Sunday 8 PM
      enabled: true
```

## Orchestration Logic

```python
async def run_full_cycle():
    """Run a complete job hunting cycle."""
    # Step 1: Search
    new_jobs = await job_searcher.search_all_platforms()

    # Step 2: Score & filter
    scored_jobs = await ai_engine.score_jobs(new_jobs)
    high_score_jobs = [j for j in scored_jobs if j.fit_score >= threshold]

    # Step 3: Instant alerts for exceptional matches
    for job in high_score_jobs:
        if job.fit_score >= 90:
            await reporter.send_instant_alert(job)

    # Step 4: Apply (if within daily limit)
    if applicant.daily_count() < max_daily_applications:
        await applicant.apply_to_jobs(high_score_jobs)

    # Step 5: Check messages
    await messenger.check_and_respond()

    # Step 6: LinkedIn engagement
    await linkedin_scanner.scan_and_engage()
```

## Health Monitoring
- Log all scheduler events to `data/jobs.db` → `scheduler_logs` table
- If a task fails 3 times consecutively, disable it and send alert
- Heartbeat ping every 30 minutes to confirm agent is running
- Expose simple health endpoint at `localhost:8080/health` (optional)

## Running Modes
```bash
# Full scheduled mode (runs until stopped)
python main.py --schedule

# Run once and exit
python main.py --run-once

# Run specific task
python main.py --task search
python main.py --task apply
python main.py --task messages
python main.py --task report

# Dry run (no actual applications or messages sent)
python main.py --schedule --dry-run
```
