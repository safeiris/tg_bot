"""Apscheduler integration for webinar reminders."""
from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import database
from config import load_settings

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


async def _send_bulk_message(application, text: str) -> None:
    participants = database.get_participants()
    for _, row in participants.iterrows():
        chat_id = int(row["ChatID"])
        if not chat_id:
            continue
        await application.bot.send_message(chat_id=chat_id, text=text)


async def _send_feedback_request(application, text: str) -> None:
    participants = database.get_participants()
    waiting_feedback = application.bot_data.setdefault("awaiting_feedback", set())
    for _, row in participants.iterrows():
        chat_id = int(row["ChatID"])
        if not chat_id:
            continue
        waiting_feedback.add(chat_id)
        await application.bot.send_message(chat_id=chat_id, text=text)


def _schedule_job(run_time: datetime, coroutine, *args) -> None:
    if run_time <= datetime.now(run_time.tzinfo):
        return
    scheduler.add_job(coroutine, trigger=DateTrigger(run_date=run_time), args=args, misfire_grace_time=300)


def schedule_all_reminders(application) -> None:
    settings = load_settings()
    event_iso = settings.get("event_datetime")
    if not event_iso:
        return

    event_dt = datetime.fromisoformat(event_iso)
    scheduler.remove_all_jobs()

    zoom_link = settings.get("zoom_link", "")

    day_before = event_dt - timedelta(days=1)
    hour_before = event_dt - timedelta(hours=1)
    day_after = event_dt + timedelta(days=1)

    text_day_before = "Напоминаем, вебинар уже завтра! 💫\n" + (f"Ваша ссылка: {zoom_link}" if zoom_link else "")
    text_hour_before = "Скоро начинаем! Вот ваша ссылка: {link}".format(link=zoom_link or "Ссылка появится позже")
    text_day_after = "Спасибо, что были с нами 💕 Поделитесь впечатлениями?"

    _schedule_job(day_before, _send_bulk_message, application, text_day_before)
    _schedule_job(hour_before, _send_bulk_message, application, text_hour_before)
    _schedule_job(day_after, _send_feedback_request, application, text_day_after)


def ensure_scheduler_started() -> None:
    if not scheduler.running:
        scheduler.start()
