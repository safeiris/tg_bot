"""Apscheduler integration for webinar reminders bound to Google Sheets events."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database
from config import TIMEZONE, load_settings
from events import classify_status, get_event
from message_templates import get_event_context

scheduler = AsyncIOScheduler(timezone=ZoneInfo(TIMEZONE))

logger = logging.getLogger(__name__)


async def _send_timed_reminder(application, event_id: str, label: str) -> None:
    event = get_event(event_id)
    if not event or classify_status(event) == "cancelled":
        return
    settings = load_settings()
    ctx = get_event_context(settings)
    zoom_link = (settings.get("zoom_link") or "").strip()

    if label == "day":
        text = (
            f"Напоминаем: уже завтра встречаемся на «{ctx['title']}».\n"
            f"Старт {ctx['local_datetime']} ({ctx['timezone']})."
        )
    else:
        text = (
            f"Через час начинаем «{ctx['title']}»!\n"
            f"Старт в {ctx['local_datetime']} ({ctx['timezone']})."
        )

    reply_markup: InlineKeyboardMarkup | None = None
    if zoom_link:
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Войти на вебинар", url=zoom_link)]]
        )
    else:
        text += "\n\nСсылка появится здесь, как только админ её добавит."

    participants = database.get_participants()
    for _, row in participants.iterrows():
        chat_id = row.get("chat_id")
        if not chat_id:
            continue
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            continue
        try:
            await application.bot.send_message(
                chat_id=chat_id_int,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Failed to send reminder to %s: %s", chat_id, exc)


async def _send_feedback_request(application, event_id: str, text: str) -> None:
    event = get_event(event_id)
    if not event or classify_status(event) == "cancelled":
        return
    participants = database.get_participants()
    waiting_feedback = application.bot_data.setdefault("awaiting_feedback", set())
    for _, row in participants.iterrows():
        chat_id = row.get("chat_id")
        if not chat_id:
            continue
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            continue
        waiting_feedback.add(chat_id_int)
        await application.bot.send_message(chat_id=chat_id_int, text=text)


def _clear_event_jobs(event_id: str) -> None:
    for job in scheduler.get_jobs():
        if job.id and job.id.startswith(f"{event_id}::"):
            scheduler.remove_job(job.id)


def _schedule_job(job_id: str, run_time: datetime, coroutine, *args) -> None:
    if run_time <= datetime.now(run_time.tzinfo):
        return
    scheduler.add_job(
        coroutine,
        trigger=DateTrigger(run_date=run_time),
        args=args,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
    )


def cancel_scheduled_reminders(event_id: str) -> None:
    _clear_event_jobs(event_id)


def schedule_all_reminders(application) -> None:
    settings = load_settings()
    event_iso = settings.get("current_event_datetime")
    event_id = settings.get("current_event_id")
    if not event_iso or not event_id:
        scheduler.remove_all_jobs()
        return

    event_dt = datetime.fromisoformat(event_iso)
    _clear_event_jobs(event_id)

    day_before = event_dt - timedelta(days=1)
    hour_before = event_dt - timedelta(hours=1)
    day_after = event_dt + timedelta(days=1)

    text_day_after = "Спасибо, что были с нами 💕 Поделитесь впечатлениями?"

    _schedule_job(
        f"{event_id}::day_before",
        day_before,
        _send_timed_reminder,
        application,
        event_id,
        "day",
    )
    _schedule_job(
        f"{event_id}::hour_before",
        hour_before,
        _send_timed_reminder,
        application,
        event_id,
        "hour",
    )
    _schedule_job(
        f"{event_id}::feedback",
        day_after,
        _send_feedback_request,
        application,
        event_id,
        text_day_after,
    )


def ensure_scheduler_started() -> None:
    if not scheduler.running:
        scheduler.start()
