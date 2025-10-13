"""Administrative tools for configuring the webinar bot."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database
from config import is_admin, load_settings, update_settings
from scheduler import ensure_scheduler_started, schedule_all_reminders

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📆 Изменить дату", "📝 Изменить тему"],
        ["🔗 Обновить Zoom", "💳 Обновить оплату"],
        ["📥 Список участников", "📢 Разослать напоминание"],
        ["🗓 Просмотр текущего мероприятия"],
    ],
    resize_keyboard=True,
)

STATE_TOPIC, STATE_DATE, STATE_ZOOM, STATE_PAYMENT, STATE_NOTIFY = range(5)


MISSING_VALUE = "❗️Не указано администратором"
_MONTH_NAMES = [
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


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and is_admin(chat_id=user.id, username=user.username))


def _format_value(value) -> str:
    if value is None:
        return MISSING_VALUE
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return MISSING_VALUE
        return stripped
    return str(value)


def _format_datetime(dt: datetime) -> str:
    if 1 <= dt.month <= 12:
        month = _MONTH_NAMES[dt.month]
    else:
        month = dt.strftime("%B")
    return f"{dt.day} {month} {dt.year}, {dt.strftime('%H:%M')}"


def _try_parse_separate_datetime(date_str: str | None, time_str: str | None) -> datetime | None:
    if not date_str:
        return None

    date_str = date_str.strip()
    time_str = (time_str or "").strip()

    if time_str:
        candidate = f"{date_str} {time_str}".strip()
        for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    else:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
    return None


async def _ensure_admin(update: Update, *, message: str = "Недостаточно прав.") -> bool:
    if _is_admin(update):
        return True
    if update.message:
        await update.message.reply_text(message)
    else:
        await update.effective_chat.send_message(message)
    return False


async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    text = (
        "<b>Панель администратора</b>\n\n"
        f"Тема: {settings.get('topic')}\n"
        f"Описание: {settings.get('description')}\n"
        f"Дата: {settings.get('event_datetime') or 'не задана'}\n"
        f"Zoom: {settings.get('zoom_link') or 'не задан'}\n"
        f"Оплата: {settings.get('payment_link') or 'не задана'}"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)
    else:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=MENU_KEYBOARD)


async def show_current_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END

    settings = load_settings()

    topic = _format_value(settings.get("topic"))
    description = _format_value(settings.get("description"))
    zoom_link = _format_value(settings.get("zoom_link"))
    payment_link = _format_value(settings.get("payment_link"))

    event_dt_text = MISSING_VALUE
    event_dt_obj: datetime | None = None

    event_iso = settings.get("event_datetime")
    if event_iso:
        try:
            event_dt_obj = datetime.fromisoformat(event_iso)
            event_dt_text = _format_datetime(event_dt_obj)
        except ValueError:
            event_dt_text = event_iso
    else:
        event_date = settings.get("event_date")
        event_time = settings.get("event_time")
        combined = " ".join(
            part.strip()
            for part in (event_date or "", event_time or "")
            if part and part.strip()
        )
        if combined:
            event_dt_text = combined
            event_dt_obj = _try_parse_separate_datetime(event_date, event_time)

    lines = [
        f"🎓 Название: {topic}",
        f"📝 Описание: {description}",
        f"📅 Дата и время: {event_dt_text if event_dt_text.strip() else MISSING_VALUE}",
        f"🔗 Zoom: {zoom_link}",
        f"💳 Ссылка на оплату: {payment_link}",
    ]

    if "timezone" in settings:
        timezone_value = _format_value(settings.get("timezone"))
        lines.append(f"🌍 Часовой пояс: {timezone_value}")

    known_fields = {
        "topic",
        "description",
        "event_datetime",
        "event_date",
        "event_time",
        "zoom_link",
        "payment_link",
        "timezone",
    }
    for key in sorted(settings):
        if key in known_fields:
            continue
        value_text = _format_value(settings.get(key))
        label = key.replace("_", " ").capitalize()
        lines.append(f"• {label}: {value_text}")

    message = "\n".join(lines)

    if event_dt_obj and event_dt_obj < datetime.now(event_dt_obj.tzinfo):
        message += "\n\n⚠️ Мероприятие уже прошло."

    if update.message:
        await update.message.reply_text(message, reply_markup=MENU_KEYBOARD)
    else:
        await update.effective_chat.send_message(message, reply_markup=MENU_KEYBOARD)

    return ConversationHandler.END


async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update, message="Эта команда доступна только администратору."):
        return ConversationHandler.END
    await send_admin_panel(update, context)
    return ConversationHandler.END


async def set_topic_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите новую тему и описание через символ \"|\".\n"
        "Например: Мой вебинар | Погружение в психологию",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_TOPIC


async def set_topic_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    payload = (update.message.text or "").split("|", maxsplit=1)
    topic = payload[0].strip() if payload else ""
    description = payload[1].strip() if len(payload) > 1 else ""
    update_settings(topic=topic or None, description=description or None)
    await update.message.reply_text("Тема и описание обновлены.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


def _parse_datetime(text: str) -> datetime:
    text = text.strip()
    formats = ["%d.%m.%Y %H:%M", "%d.%m %H:%M"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    raise ValueError("invalid format")


async def set_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_DATE


async def set_date_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    try:
        event_dt = _parse_datetime(text)
    except ValueError:
        await update.message.reply_text("Не удалось распознать дату. Попробуйте снова.")
        return STATE_DATE

    update_settings(event_datetime=event_dt.isoformat())
    await update.message.reply_text("Дата вебинара обновлена.", reply_markup=MENU_KEYBOARD)
    ensure_scheduler_started()
    schedule_all_reminders(context.application)
    return ConversationHandler.END


async def set_zoom_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Отправьте новую Zoom-ссылку:", reply_markup=ReplyKeyboardRemove())
    return STATE_ZOOM


async def set_zoom_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    update_settings(zoom_link=link)
    await update.message.reply_text("Ссылка Zoom обновлена.", reply_markup=MENU_KEYBOARD)
    schedule_all_reminders(context.application)
    return ConversationHandler.END


async def set_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Отправьте ссылку на оплату (Robokassa):", reply_markup=ReplyKeyboardRemove())
    return STATE_PAYMENT


async def set_payment_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    update_settings(payment_link=link)
    await update.message.reply_text("Ссылка на оплату обновлена.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


async def export_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    path = database.export_database()
    await update.message.reply_document(document=path.read_bytes(), filename=Path(path).name)
    return ConversationHandler.END


async def notify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите текст напоминания, который будет отправлен всем участникам:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_NOTIFY


async def notify_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    participants = database.list_chat_ids()
    for chat_id in participants:
        await context.bot.send_message(chat_id=chat_id, text=text)
    await update.message.reply_text("Рассылка отправлена.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


def build_admin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_command_entry),
            CommandHandler("set_topic", set_topic_start),
            CommandHandler("set_date", set_date_start),
            CommandHandler("set_zoom", set_zoom_start),
            CommandHandler("set_payment", set_payment_start),
            CommandHandler("export", export_participants),
            CommandHandler("notify", notify_start),
            CommandHandler("current_event", show_current_event),
            MessageHandler(filters.Regex("^📝"), set_topic_start),
            MessageHandler(filters.Regex("^📆"), set_date_start),
            MessageHandler(filters.Regex("^🔗"), set_zoom_start),
            MessageHandler(filters.Regex("^💳"), set_payment_start),
            MessageHandler(filters.Regex("^📥"), export_participants),
            MessageHandler(filters.Regex("^📢"), notify_start),
            MessageHandler(filters.Regex("^🗓"), show_current_event),
        ],
        states={
            STATE_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_topic_finish)],
            STATE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_date_finish)],
            STATE_ZOOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_zoom_finish)],
            STATE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_payment_finish)],
            STATE_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, notify_finish)],
        },
        fallbacks=[CommandHandler("cancel", admin_command_entry)],
        allow_reentry=True,
    )
