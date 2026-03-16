"""Job Auto-Apply Agent — Entry Point."""

import argparse
import asyncio
import logging
import sys

from src import db
from src.config import DRY_RUN


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/agent.log", mode="a"),
    ],
)
logger = logging.getLogger("job-agent")


async def run_once():
    """Run a full job hunting cycle once."""
    from src import job_searcher, applicant, linkedin_scanner, messenger, reporter

    logger.info("=== Starting full job hunting cycle ===")
    if DRY_RUN:
        logger.info("*** DRY RUN MODE — no real applications will be sent ***")

    logger.info("Step 1/5: Searching for jobs...")
    new_jobs = await job_searcher.search_all_platforms()
    logger.info(f"Found {len(new_jobs)} new jobs")

    # Instant alerts for high scores
    for job in new_jobs:
        await reporter.send_instant_alert(job)

    logger.info("Step 2/5: Applying to top matches...")
    await applicant.apply_to_jobs()

    logger.info("Step 3/5: Scanning LinkedIn hiring posts...")
    await linkedin_scanner.scan_and_engage()

    logger.info("Step 4/5: Checking messages...")
    await messenger.check_and_respond()

    logger.info("Step 5/5: Sending report...")
    report = await reporter.send_daily_report()
    logger.info(f"Report:\n{report}")

    logger.info("=== Cycle complete ===")


async def run_scheduled():
    """Run the scheduler continuously."""
    from src.scheduler import create_scheduler

    logger.info("Starting scheduled mode...")
    scheduler = create_scheduler()
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")


async def run_task(task_name: str):
    """Run a specific task."""
    from src import job_searcher, applicant, linkedin_scanner, messenger, reporter

    task_map = {
        "search": job_searcher.search_all_platforms,
        "apply": applicant.apply_to_jobs,
        "linkedin": linkedin_scanner.scan_and_engage,
        "messages": messenger.check_and_respond,
        "report": reporter.send_daily_report,
    }
    func = task_map.get(task_name)
    if not func:
        logger.error(f"Unknown task: {task_name}. Available: {list(task_map.keys())}")
        return
    logger.info(f"Running task: {task_name}")
    await func()


def main():
    parser = argparse.ArgumentParser(description="Job Auto-Apply Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-once", action="store_true", help="Run full cycle once")
    group.add_argument("--schedule", action="store_true", help="Run on schedule")
    group.add_argument("--task", type=str, help="Run specific task (search/apply/linkedin/messages/report)")
    group.add_argument("--search-only", action="store_true", help="Search only")
    group.add_argument("--apply-only", action="store_true", help="Apply only")
    group.add_argument("--report-only", action="store_true", help="Report only")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")

    args = parser.parse_args()

    if args.dry_run:
        import os
        os.environ["DRY_RUN"] = "true"

    asyncio.run(_async_main(args))


async def _async_main(args):
    await db.init_db()

    if args.run_once:
        await run_once()
    elif args.schedule:
        await run_scheduled()
    elif args.task:
        await run_task(args.task)
    elif args.search_only:
        await run_task("search")
    elif args.apply_only:
        await run_task("apply")
    elif args.report_only:
        await run_task("report")


if __name__ == "__main__":
    main()
