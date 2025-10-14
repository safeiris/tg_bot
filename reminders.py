"""Personal reminder scheduling helpers for inline user actions."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from zoneinfo import ZoneInfo

import database
from config import TIMEZONE
from events import classify_status, get_event


logger = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)

REMINDER_DAY_LABEL = "day"
REMINDER_HOUR_LABEL = "hour"
REMINDER_LABELS: tuple[tuple[str, timedelta], ...] = (
    (REMINDER_DAY_LABEL, timedelta(days=1)),
    (REMINDER_HOUR_LABEL, timedelta(hours=1)),
)


def _user_job_prefix(event_id: str) -> str:
    return f"event::{event_id}::user::"


def _user_job_name(event_id: str, chat_id: int, label: str) -> str:
    return f"{_user_job_prefix(event_id)}{chat_id}::{label}"


def _resolve_event_datetime(event) -> Optional[datetime]:
    if event is None:
        return None
    event_dt = event.parsed_datetime
    if event_dt is None:
        return None
    return event_dt.astimezone(TZ)


def _format_event_start(event) -> tuple[str, str]:
    title = getattr(event, "title", "") or "—"
    dt = _resolve_event_datetime(event)
    if dt is None:
        return title, "—"
    tz_label = getattr(event, "timezone", None) or TIMEZONE
    return title, f"{dt.strftime('%d.%m.%Y %H:%M')} ({tz_label})"


def _build_user_reminder_payload(
    event,
    label: str,
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    title, start_text = _format_event_start(event)
    if label == REMINDER_DAY_LABEL:
        text = (
            f"Напоминаем: уже завтра встречаемся на «{title}».\n"
            f"Старт {start_text}."
        )
    else:
        text = (
            f"Через час начинаем «{title}»!\n"
            f"Старт в {start_text}."
        )
    zoom_link = (getattr(event, "zoom_url", "") or "").strip()
    reply_markup: Optional[InlineKeyboardMarkup] = None
    if zoom_link:
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Войти на вебинар", url=zoom_link)]]
        )
    else:
        text += "\n\nСсылка появится здесь, как только админ её добавит."
    return text, reply_markup


def _cancel_event_jobs(job_queue, event_id: Optional[str], chat_id: Optional[int] = None) -> None:
    for job in list(job_queue.jobs()):
        name = job.name or ""
        data = job.data if isinstance(job.data, dict) else {}
        if event_id:
            if chat_id is not None:
                prefix = f"{_user_job_prefix(event_id)}{chat_id}::"
                if name.startswith(prefix) or (
                    data.get("event_id") == event_id and data.get("chat_id") == chat_id
                ):
                    job.schedule_removal()
            else:
                prefix = _user_job_prefix(event_id)
                if name.startswith(prefix) or data.get("event_id") == event_id:
                    job.schedule_removal()
        else:
            if name.startswith("event::"):
                job.schedule_removal()


def _schedule_event_jobs_for_chat(
    job_queue,
    *,
    event_id: str,
    chat_id: int,
    event_dt: datetime,
    now: datetime,
) -> list[datetime]:
    scheduled: list[datetime] = []
    for label, delta in REMINDER_LABELS:
        run_at = event_dt - delta
        if run_at <= now:
            continue
        job_queue.run_once(
            _deliver_user_event_reminder,
            when=run_at,
            data={"chat_id": chat_id, "event_id": event_id, "label": label},
            name=_user_job_name(event_id, chat_id, label),
            chat_id=chat_id,
        )
        scheduled.append(run_at)
    return scheduled


async def _deliver_user_event_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None:
        return
    data = job.data if isinstance(job.data, dict) else {}
    chat_id = data.get("chat_id")
    event_id = data.get("event_id")
    label = data.get("label")
    if not chat_id or not event_id or label not in {REMINDER_DAY_LABEL, REMINDER_HOUR_LABEL}:
        return
    event = get_event(event_id)
    if not event:
        return
    status = classify_status(event)
    if status in {"cancelled", "past"}:
        return
    text, reply_markup = _build_user_reminder_payload(event, label)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Failed to send personal event reminder to %s: %s", chat_id, exc)


def plan_user_event_reminders(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    event_id: str,
) -> list[datetime]:
    application = context.application
    if application is None or application.job_queue is None:
        return []
    job_queue = application.job_queue
    _cancel_event_jobs(job_queue, event_id, chat_id)
    event = get_event(event_id)
    if not event:
        return []
    if classify_status(event) != "active":
        return []
    event_dt = _resolve_event_datetime(event)
    if event_dt is None:
        return []
    now = datetime.now(event_dt.tzinfo or TZ)
    return _schedule_event_jobs_for_chat(
        job_queue,
        event_id=event_id,
        chat_id=chat_id,
        event_dt=event_dt,
        now=now,
    )


def cancel_event_user_reminders(application: Optional[Application], event_id: Optional[str]) -> None:
    if application is None or application.job_queue is None:
        return
    _cancel_event_jobs(application.job_queue, event_id)


def replan_event_user_reminders(application: Optional[Application], event_id: str) -> None:
    if application is None or application.job_queue is None:
        return
    job_queue = application.job_queue
    _cancel_event_jobs(job_queue, event_id)
    event = get_event(event_id)
    if not event:
        return
    if classify_status(event) != "active":
        return
    event_dt = _resolve_event_datetime(event)
    if event_dt is None:
        return
    now = datetime.now(event_dt.tzinfo or TZ)
    for chat_id in database.list_chat_ids():
        _schedule_event_jobs_for_chat(
            job_queue,
            event_id=event_id,
            chat_id=chat_id,
            event_dt=event_dt,
            now=now,
        )


async def _deliver_personal_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None:
        return
    data = job.data or {}
    chat_id = data.get("chat_id")
    message = data.get("message")
    event_id = data.get("event_id")
    if event_id:
        event = get_event(event_id)
        if not event or classify_status(event) == "cancelled":
            return
    if not chat_id or not message:
        return
    await context.bot.send_message(chat_id=chat_id, text=message)


def schedule_personal_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    run_at: datetime,
    message: str,
    label: str,
    event_id: Optional[str] = None,
) -> Optional[datetime]:
    """Schedule a per-user reminder, replacing previous jobs with the same label."""

    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=TZ)
    if run_at <= datetime.now(run_at.tzinfo):
        return None

    job_queue = context.application.job_queue
    job_name = (
        f"event::{event_id}::user::{chat_id}::{label}" if event_id else f"user::{chat_id}::{label}"
    )
    for job in list(job_queue.jobs()):
        name = job.name or ""
        if name.endswith(f"::{chat_id}::{label}"):
            job.schedule_removal()

    job_data = {"chat_id": chat_id, "message": message}
    if event_id:
        job_data["event_id"] = event_id
    job_queue.run_once(
        _deliver_personal_reminder,
        when=run_at,
        data=job_data,
        name=job_name,
        chat_id=chat_id,
    )
    return run_at


def cancel_personal_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, label: str) -> None:
    job_queue = context.application.job_queue
    suffix = f"::{chat_id}::{label}"
    for job in list(job_queue.jobs()):
        name = job.name or ""
        if name.endswith(suffix):
            job.schedule_removal()
