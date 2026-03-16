"""APScheduler-based cron runner for all job hunting tasks."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import load_profile
from src import job_searcher, applicant, linkedin_scanner, messenger, reporter, db

logger = logging.getLogger(__name__)


def _parse_cron(cron_str: str) -> dict:
    """Parse '0 8 * * *' into APScheduler CronTrigger kwargs."""
    parts = cron_str.split()
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


async def _log_task(name: str, status: str, details: str = ""):
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO scheduler_logs (task_name, status, details) VALUES (?, ?, ?)",
        (name, status, details),
    )
    await conn.commit()
    await conn.close()


async def _run_search():
    try:
        await _log_task("search", "started")
        jobs = await job_searcher.search_all_platforms()
        # Send instant alerts for high-score jobs
        for job in jobs:
            await reporter.send_instant_alert(job)
        await _log_task("search", "completed", f"Found {len(jobs)} new jobs")
    except Exception as e:
        logger.error(f"Search task failed: {e}")
        await _log_task("search", "failed", str(e))


async def _run_apply():
    try:
        await _log_task("apply", "started")
        await applicant.apply_to_jobs()
        await _log_task("apply", "completed")
    except Exception as e:
        logger.error(f"Apply task failed: {e}")
        await _log_task("apply", "failed", str(e))


async def _run_linkedin_scan():
    try:
        await _log_task("linkedin_scan", "started")
        await linkedin_scanner.scan_and_engage()
        await _log_task("linkedin_scan", "completed")
    except Exception as e:
        logger.error(f"LinkedIn scan failed: {e}")
        await _log_task("linkedin_scan", "failed", str(e))


async def _run_messages():
    try:
        await _log_task("check_messages", "started")
        await messenger.check_and_respond()
        await _log_task("check_messages", "completed")
    except Exception as e:
        logger.error(f"Message check failed: {e}")
        await _log_task("check_messages", "failed", str(e))


async def _run_daily_report():
    try:
        await _log_task("daily_report", "started")
        await reporter.send_daily_report()
        await _log_task("daily_report", "completed")
    except Exception as e:
        logger.error(f"Daily report failed: {e}")
        await _log_task("daily_report", "failed", str(e))


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler with all tasks."""
    profile = load_profile()
    tz = profile.get("scheduler", {}).get("timezone", "US/Eastern")
    jobs_cfg = profile.get("scheduler", {}).get("jobs", {})

    scheduler = AsyncIOScheduler(timezone=tz)

    task_map = {
        "search": _run_search,
        "apply": _run_apply,
        "linkedin_scan": _run_linkedin_scan,
        "check_messages": _run_messages,
        "daily_report": _run_daily_report,
        "weekly_report": _run_daily_report,  # reuses daily but could be extended
    }

    for task_name, cfg in jobs_cfg.items():
        if not cfg.get("enabled", False):
            continue
        func = task_map.get(task_name)
        if not func:
            continue
        cron_kwargs = _parse_cron(cfg["cron"])
        scheduler.add_job(
            func,
            trigger=CronTrigger(**cron_kwargs, timezone=tz),
            id=task_name,
            name=task_name,
            replace_existing=True,
        )
        logger.info(f"Scheduled task: {task_name} — cron: {cfg['cron']}")

    return scheduler
