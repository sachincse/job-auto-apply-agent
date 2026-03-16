"""Auto-read and respond to recruiter messages on LinkedIn."""

import asyncio
import logging

from src.config import load_profile, DRY_RUN
from src import ai_engine, db
from src.browser import get_browser, linkedin_login, _human_delay

logger = logging.getLogger(__name__)


async def check_and_respond():
    """Check LinkedIn messages and draft/send responses."""
    profile = load_profile()
    msg_cfg = profile.get("messaging", {})
    auto_send = msg_cfg.get("auto_send_interested", False)
    min_score = msg_cfg.get("min_fit_score_to_reply", 50)

    async with get_browser() as page:
        await linkedin_login(page)

        # Navigate to messaging
        await page.goto("https://www.linkedin.com/messaging/")
        await asyncio.sleep(_human_delay(3, 5))

        # Get unread conversations
        unread_items = await page.query_selector_all(
            '.msg-conversation-listitem--unread'
        )
        logger.info(f"Found {len(unread_items)} unread conversations")

        for item in unread_items[:10]:  # Process max 10
            try:
                await item.click()
                await asyncio.sleep(_human_delay(2, 4))

                # Read sender name
                name_el = await page.query_selector(
                    '.msg-entity-lockup__entity-title'
                )
                sender_name = await name_el.inner_text() if name_el else "Unknown"

                # Read message content
                messages = await page.query_selector_all('.msg-s-message-list__event')
                if not messages:
                    continue

                last_msg_el = messages[-1]
                msg_body = await last_msg_el.query_selector('.msg-s-event-listitem__body')
                message_text = await msg_body.inner_text() if msg_body else ""

                if not message_text.strip():
                    continue

                logger.info(f"Message from {sender_name}: {message_text[:100]}...")

                # Classify and draft reply
                result = ai_engine.classify_message(message_text)
                classification = result["classification"]
                fit_score = result["fit_score"]
                draft_reply = result["draft_reply"]

                logger.info(
                    f"Classified as: {classification}, Fit: {fit_score}, "
                    f"Draft: {draft_reply[:80]}..."
                )

                # Store in DB
                await db.insert_message({
                    "source": "linkedin",
                    "sender_name": sender_name,
                    "sender_profile": "",
                    "message_content": message_text,
                    "classification": classification,
                    "fit_score": fit_score,
                    "draft_reply": draft_reply,
                })

                # Send reply if conditions met
                should_send = (
                    auto_send
                    and not DRY_RUN
                    and classification == "job_opportunity"
                    and fit_score >= min_score
                )

                if should_send:
                    msg_input = await page.query_selector('.msg-form__contenteditable')
                    if msg_input:
                        await msg_input.fill(draft_reply)
                        await asyncio.sleep(_human_delay(1, 2))
                        send_btn = await page.query_selector('.msg-form__send-button')
                        if send_btn:
                            await send_btn.click()
                            logger.info(f"Sent reply to {sender_name}")
                            await asyncio.sleep(_human_delay(2, 4))
                else:
                    logger.info(
                        f"Draft saved for {sender_name} "
                        f"(auto_send={auto_send}, class={classification}, score={fit_score})"
                    )

            except Exception as e:
                logger.error(f"Error processing message: {e}")
                continue

            await asyncio.sleep(_human_delay(2, 5))
