"""Entry point for the psychology webinar Telegram bot."""
from __future__ import annotations

from telegram.ext import Application, MessageHandler, filters

import config
import database
from admin_panel import build_admin_conversation
from handlers import build_conversation_handler, feedback_handler
from scheduler import ensure_scheduler_started, schedule_all_reminders


async def _post_init(application: Application) -> None:
    config.ensure_data_dir()
    database.ensure_database()
    ensure_scheduler_started()
    schedule_all_reminders(application)


def main() -> None:
    application = Application.builder().token(config.BOT_TOKEN).build()
    application.post_init = _post_init

    application.add_handler(build_conversation_handler())
    application.add_handler(build_admin_conversation())
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
