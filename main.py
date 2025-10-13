"""Entry point for the psychology webinar Telegram bot."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database
from admin_panel import admin_command_entry, handle_admin_callback, handle_admin_message
from handlers import (
    build_conversation_handler,
    feedback_handler,
    handle_user_callback,
)
from scheduler import ensure_scheduler_started, schedule_all_reminders


logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    config.ensure_data_dir()
    ensure_scheduler_started()
    schedule_all_reminders(application)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if exc:
        logger.error(
            "Unhandled exception while processing update: %s",
            update,
            exc_info=exc,
        )
    else:
        logger.error("Unhandled error without exception while processing update: %s", update)
    chat_id = None
    if isinstance(update, Update):
        chat = update.effective_chat
        if chat:
            chat_id = chat.id
    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Внутренняя ошибка. Мы уже разбираемся.",
            )
        except Exception as send_error:
            logger.warning(
                "Failed to notify chat %s about internal error: %s",
                chat_id,
                send_error,
            )


def main() -> None:
    application = Application.builder().token(config.BOT_TOKEN).build()
    application.post_init = _post_init

    application.add_handler(build_conversation_handler())
    application.add_handler(CommandHandler("admin", admin_command_entry))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^(?:admin:|nav:back$)"))
    application.add_handler(CallbackQueryHandler(handle_user_callback, pattern=r"^user:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler))
    application.add_error_handler(_error_handler)

    application.run_polling()


if __name__ == "__main__":
    main()
