"""Utilities for generating user-facing messages with event details."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from zoneinfo import ZoneInfo

from config import TIMEZONE, load_settings

MISSING_VALUE = "❗️Не указано администратором"
MONTH_NAMES = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
TZ = ZoneInfo(TIMEZONE)


def _format_value(value: Optional[object]) -> str:
    if value is None:
        return MISSING_VALUE
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else MISSING_VALUE
    return str(value)


def _format_datetime(event_iso: Optional[object]) -> str:
    if not event_iso:
        return MISSING_VALUE
    try:
        dt = datetime.fromisoformat(str(event_iso))
    except ValueError:
        return MISSING_VALUE
    local_dt = dt.astimezone(TZ)
    if 1 <= local_dt.month < len(MONTH_NAMES):
        month_name = MONTH_NAMES[local_dt.month]
    else:
        month_name = local_dt.strftime("%B")
    return f"{local_dt.day} {month_name} {local_dt.year}, {local_dt.strftime('%H:%M')}"


def get_event_context(settings: Optional[Dict[str, object]] = None) -> Dict[str, str]:
    """Return event-related placeholders for user messages."""

    if settings is None:
        settings = load_settings()

    topic = _format_value(settings.get("topic"))
    description = _format_value(settings.get("description"))
    payment_link = _format_value(settings.get("payment_link"))

    local_dt = _format_datetime(settings.get("current_event_datetime"))
    timezone_value = _format_value(settings.get("timezone"))
    if timezone_value == MISSING_VALUE:
        timezone_value = TIMEZONE

    return {
        "title": topic,
        "description": description,
        "payment_link": payment_link,
        "local_datetime": local_dt,
        "timezone": timezone_value,
    }


def build_free_confirmation(settings: Optional[Dict[str, object]] = None) -> str:
    ctx = get_event_context(settings)
    return (
        "✅ Регистрация сохранена!\n\n"
        f"🧠 {ctx['title']}\n"
        f"📅 {ctx['local_datetime']} ({ctx['timezone']})\n"
        f"📝 {ctx['description']}\n\n"
        "👤 Тип участия: Наблюдатель (бесплатно)\n"
        "Мы пришлём напоминание за 1 день и за 1 час до начала мероприятия."
    )


def build_paid_pending_confirmation(settings: Optional[Dict[str, object]] = None) -> str:
    ctx = get_event_context(settings)
    return (
        "🧾 Участие с разбором\n\n"
        f"🧠 {ctx['title']}\n"
        f"📅 {ctx['local_datetime']} ({ctx['timezone']})\n\n"
        "Для подтверждения участия внесите оплату по ссылке:\n"
        f"{ctx['payment_link']}\n\n"
        "После оплаты нажмите кнопку «Я оплатил», чтобы мы зафиксировали ваш статус.\n"
        "Мы пришлём напоминание за 1 день и за 1 час до начала мероприятия."
    )


def build_paid_confirmation(settings: Optional[Dict[str, object]] = None) -> str:
    ctx = get_event_context(settings)
    return (
        "✅ Оплата получена\n\n"
        f"🧠 {ctx['title']}\n"
        f"📅 {ctx['local_datetime']} ({ctx['timezone']})\n"
        "👤 Тип участия: Участник с разбором (платно)\n"
        "💳 Статус оплаты: Оплачено\n\n"
        "Мы пришлём напоминание за 1 день и за 1 час до начала мероприятия."
    )
