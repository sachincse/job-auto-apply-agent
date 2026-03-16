"""SQLite database for tracking jobs, applications, messages, and engagements."""

import aiosqlite
from src.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS searched_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    status TEXT DEFAULT 'new',
    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES searched_jobs(id),
    cover_letter TEXT,
    screenshot_path TEXT,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'submitted',
    response TEXT
);

CREATE TABLE IF NOT EXISTS linkedin_engagements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_url TEXT UNIQUE,
    author_name TEXT,
    post_content TEXT,
    relevance_score INTEGER,
    action_taken TEXT,
    comment_text TEXT,
    dm_text TEXT,
    engaged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    sender_name TEXT,
    sender_profile TEXT,
    message_content TEXT,
    classification TEXT,
    fit_score INTEGER,
    draft_reply TEXT,
    reply_sent INTEGER DEFAULT 0,
    sent_at TIMESTAMP,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduler_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT,
    status TEXT,
    details TEXT,
    ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()
    await db.executescript(SCHEMA)
    await db.commit()
    await db.close()


async def job_exists(external_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM searched_jobs WHERE external_id = ?", (external_id,)
    )
    row = await cursor.fetchone()
    await db.close()
    return row is not None


async def insert_job(job: dict) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT OR IGNORE INTO searched_jobs
           (external_id, title, company, location, url, source,
            salary_min, salary_max, description, fit_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["external_id"], job["title"], job["company"],
            job["location"], job["url"], job["source"],
            job.get("salary_min"), job.get("salary_max"),
            job.get("description", ""), job.get("fit_score", 0),
        ),
    )
    await db.commit()
    job_id = cursor.lastrowid
    await db.close()
    return job_id


async def get_jobs_to_apply(threshold: int = 70, limit: int = 10) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM searched_jobs
           WHERE fit_score >= ? AND status = 'new'
           ORDER BY fit_score DESC LIMIT ?""",
        (threshold, limit),
    )
    rows = await cursor.fetchall()
    await db.close()
    return [dict(r) for r in rows]


async def mark_applied(job_id: int, cover_letter: str, screenshot_path: str = ""):
    db = await get_db()
    await db.execute(
        "UPDATE searched_jobs SET status = 'applied' WHERE id = ?", (job_id,)
    )
    await db.execute(
        """INSERT INTO applications (job_id, cover_letter, screenshot_path)
           VALUES (?, ?, ?)""",
        (job_id, cover_letter, screenshot_path),
    )
    await db.commit()
    await db.close()


async def insert_message(msg: dict) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO messages
           (source, sender_name, sender_profile, message_content,
            classification, fit_score, draft_reply)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            msg["source"], msg["sender_name"], msg.get("sender_profile", ""),
            msg["message_content"], msg.get("classification", ""),
            msg.get("fit_score", 0), msg.get("draft_reply", ""),
        ),
    )
    await db.commit()
    msg_id = cursor.lastrowid
    await db.close()
    return msg_id


async def get_daily_stats() -> dict:
    db = await get_db()
    stats = {}
    for key, query in {
        "jobs_found": "SELECT COUNT(*) FROM searched_jobs WHERE DATE(searched_at) = DATE('now')",
        "applied_today": "SELECT COUNT(*) FROM applications WHERE DATE(applied_at) = DATE('now')",
        "total_applied": "SELECT COUNT(*) FROM applications",
        "messages_received": "SELECT COUNT(*) FROM messages WHERE DATE(received_at) = DATE('now')",
        "replies_sent": "SELECT COUNT(*) FROM messages WHERE reply_sent = 1 AND DATE(sent_at) = DATE('now')",
        "posts_engaged": "SELECT COUNT(*) FROM linkedin_engagements WHERE DATE(engaged_at) = DATE('now')",
    }.items():
        cursor = await db.execute(query)
        row = await cursor.fetchone()
        stats[key] = row[0] if row else 0
    await db.close()
    return stats
