"""High level notifications sent to participants."""
from __future__ import annotations

from typing import Optional

from telegram import Bot

import database
from message_templates import build_paid_confirmation


async def send_paid_confirmation(
    bot: Bot, chat_id: int, *, update_status: bool = True, settings: Optional[dict] = None
) -> None:
    """Send the final confirmation to a paid participant.

    Args:
        bot: Telegram bot instance used to deliver the message.
        chat_id: Recipient chat identifier.
        update_status: Whether to update the participant record as paid.
        settings: Optional settings snapshot to reuse for message placeholders.
    """

    if update_status:
        try:
            database.update_participation(chat_id, "paid", "yes")
        except RuntimeError:
            # If the sheet is temporarily unavailable we still try to notify the user.
            pass
    text = build_paid_confirmation(settings)
    await bot.send_message(chat_id=chat_id, text=text)
