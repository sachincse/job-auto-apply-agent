"""Scan LinkedIn posts for hiring signals and engage with relevant ones."""

import asyncio
import logging

from src.config import load_profile, DRY_RUN
from src import ai_engine, db
from src.browser import (
    get_browser, linkedin_login, linkedin_search_hiring_posts, _human_delay,
)

logger = logging.getLogger(__name__)

SEARCH_QUERIES = [
    '"we\'re hiring" AND "{keyword}"',
    '"looking for" AND "{keyword}" AND "remote"',
    '"open role" AND "{keyword}"',
    '"DM me" AND "{keyword}"',
    '#hiring #{keyword_no_space}',
]


async def scan_and_engage():
    """Search LinkedIn for hiring posts and engage with relevant ones."""
    profile = load_profile()
    keywords = profile["job_search"]["keywords"]

    async with get_browser() as page:
        await linkedin_login(page)

        comments_today = 0
        dms_today = 0
        max_comments = 10
        max_dms = 5

        for keyword in keywords[:3]:  # limit to top 3 keywords
            for query_template in SEARCH_QUERIES[:2]:  # limit queries
                query = query_template.format(
                    keyword=keyword,
                    keyword_no_space=keyword.replace(" ", ""),
                )
                logger.info(f"Searching LinkedIn posts: {query}")

                posts = await linkedin_search_hiring_posts(page, query)

                for post in posts:
                    # Check if already engaged
                    post_db = await _check_post_exists(post["url"])
                    if post_db:
                        continue

                    # Score relevance
                    score = ai_engine.score_linkedin_post(post["content"])

                    if score < 70:
                        await _save_engagement(post, score, "skipped")
                        continue

                    logger.info(
                        f"Relevant post (score {score}) by {post['author']}: "
                        f"{post['content'][:100]}..."
                    )

                    # Generate comment
                    comment = ai_engine.generate_comment(post["content"])
                    action = "commented"

                    if not DRY_RUN and comments_today < max_comments:
                        # Like the post
                        like_btn = await page.query_selector(
                            'button[aria-label*="Like"]'
                        )
                        if like_btn:
                            await like_btn.click()
                            await asyncio.sleep(_human_delay(1, 2))

                        # Post comment
                        comment_btn = await page.query_selector(
                            'button[aria-label*="Comment"]'
                        )
                        if comment_btn:
                            await comment_btn.click()
                            await asyncio.sleep(_human_delay(1, 2))
                            comment_box = await page.query_selector(
                                '.ql-editor[contenteditable="true"]'
                            )
                            if comment_box:
                                await comment_box.fill(comment)
                                await asyncio.sleep(_human_delay(1, 2))
                                submit_btn = await page.query_selector(
                                    'button.comments-comment-box__submit-button'
                                )
                                if submit_btn:
                                    await submit_btn.click()
                                    comments_today += 1
                                    await asyncio.sleep(_human_delay(2, 4))
                    else:
                        action = "drafted"
                        logger.info(f"[DRY RUN] Would comment: {comment}")

                    # Check if DM is invited
                    dm_text = ""
                    if any(
                        phrase in post["content"].lower()
                        for phrase in ["dm me", "message me", "reach out", "send me"]
                    ):
                        if dms_today < max_dms:
                            dm_text = f"Hi {post['author'].split()[0]}, saw your post about hiring. {comment}"
                            action = "dm_sent" if not DRY_RUN else "dm_drafted"
                            dms_today += 1

                    await _save_engagement(post, score, action, comment, dm_text)
                    await asyncio.sleep(_human_delay(3, 8))

    logger.info(f"LinkedIn scan complete. Comments: {comments_today}, DMs: {dms_today}")


async def _check_post_exists(url: str) -> bool:
    if not url:
        return False
    conn = await db.get_db()
    cursor = await conn.execute(
        "SELECT 1 FROM linkedin_engagements WHERE post_url = ?", (url,)
    )
    row = await cursor.fetchone()
    await conn.close()
    return row is not None


async def _save_engagement(
    post: dict, score: int, action: str,
    comment: str = "", dm: str = "",
):
    conn = await db.get_db()
    await conn.execute(
        """INSERT OR IGNORE INTO linkedin_engagements
           (post_url, author_name, post_content, relevance_score,
            action_taken, comment_text, dm_text)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            post.get("url", ""), post.get("author", ""),
            post.get("content", "")[:2000], score,
            action, comment, dm,
        ),
    )
    await conn.commit()
    await conn.close()
