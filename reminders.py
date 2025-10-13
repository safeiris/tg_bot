"""Personal reminder scheduling helpers for inline user actions."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from telegram.ext import ContextTypes

from zoneinfo import ZoneInfo

from config import TIMEZONE
from events import classify_status, get_event

TZ = ZoneInfo(TIMEZONE)


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
