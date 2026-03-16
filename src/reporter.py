"""Send reports via Email and Telegram."""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from telegram import Bot

from src.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, REPORT_EMAIL_TO,
    load_profile,
)
from src import db

logger = logging.getLogger(__name__)


async def _build_report() -> str:
    """Build the daily report text."""
    stats = await db.get_daily_stats()

    # Get top jobs today
    conn = await db.get_db()
    cursor = await conn.execute(
        """SELECT title, company, fit_score, status FROM searched_jobs
           WHERE DATE(searched_at) = DATE('now')
           ORDER BY fit_score DESC LIMIT 5"""
    )
    top_jobs = await cursor.fetchall()
    await conn.close()

    top_jobs_text = ""
    for j in top_jobs:
        top_jobs_text += f"  - {j[0]} @ {j[1]} — Score: {j[2]} — {j[3]}\n"

    if not top_jobs_text:
        top_jobs_text = "  No new jobs found today\n"

    report = f"""
--- Job Hunt Report ---

SEARCH SUMMARY
  Jobs found today: {stats['jobs_found']}

APPLICATIONS
  Applied today: {stats['applied_today']}
  Total applied (all time): {stats['total_applied']}

LINKEDIN ENGAGEMENT
  Posts engaged: {stats['posts_engaged']}

MESSAGES
  New messages: {stats['messages_received']}
  Replies sent: {stats['replies_sent']}

TOP MATCHES TODAY
{top_jobs_text}
""".strip()

    return report


async def send_telegram(message: str):
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping")
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Telegram report sent")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


async def send_email(subject: str, body: str):
    """Send a report via email."""
    if not SMTP_USER or not REPORT_EMAIL_TO:
        logger.warning("Email not configured, skipping")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = REPORT_EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email report sent")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


async def send_daily_report():
    """Generate and send the daily report via all configured channels."""
    report = await _build_report()
    profile = load_profile()
    channels = profile.get("reporting", {}).get("channels", {})

    if channels.get("telegram"):
        await send_telegram(report)
    if channels.get("email"):
        await send_email("Job Hunt Daily Report", report)

    logger.info("Daily report sent")
    return report


async def send_instant_alert(job: dict):
    """Send an instant Telegram alert for a high-score job."""
    profile = load_profile()
    threshold = (
        profile.get("reporting", {})
        .get("telegram", {})
        .get("alert_threshold", 90)
    )
    if job.get("fit_score", 0) < threshold:
        return

    alert = (
        f"HIGH MATCH JOB ALERT\n\n"
        f"Role: {job['title']} @ {job['company']}\n"
        f"Location: {job.get('location', 'N/A')}\n"
        f"Fit Score: {job['fit_score']}/100\n"
        f"Source: {job['source']}\n\n"
        f"Link: {job['url']}"
    )
    await send_telegram(alert)
