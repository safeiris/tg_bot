"""Administrative tools for configuring the webinar bot."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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

BUTTON_SET_DATE = "📆 Изменить дату"
BUTTON_SET_TOPIC = "✏️ Изменить название"
BUTTON_SET_DESCRIPTION = "📝 Изменить описание"
BUTTON_SET_ZOOM = "🔗 Обновить Zoom"
BUTTON_SET_PAYMENT = "💳 Обновить оплату"
BUTTON_EXPORT = "📥 Список участников"
BUTTON_NOTIFY = "📢 Разослать напоминание"
BUTTON_SHOW_EVENT = "🗓 Просмотр текущего мероприятия"

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BUTTON_SET_DATE, BUTTON_SET_TOPIC],
        [BUTTON_SET_DESCRIPTION],
        [BUTTON_SET_ZOOM, BUTTON_SET_PAYMENT],
        [BUTTON_EXPORT, BUTTON_NOTIFY],
        [BUTTON_SHOW_EVENT],
    ],
    resize_keyboard=True,
)

STATE_TOPIC, STATE_DATE, STATE_ZOOM, STATE_PAYMENT, STATE_NOTIFY, STATE_DESCRIPTION = range(6)

CANCEL_TEXT = "отмена"
CLEAR_TEXT = "очистить"
TOPIC_MAX_LENGTH = 200


def _normalize_command_text(text: str | None) -> str:
    return (text or "").strip().lower()


def _is_cancel(text: str | None) -> bool:
    return _normalize_command_text(text) == CANCEL_TEXT


def _is_clear(text: str | None) -> bool:
    return _normalize_command_text(text) == CLEAR_TEXT


def _is_valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc)


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
        f"Название: {_format_value(settings.get('topic'))}\n"
        f"Описание: {_format_value(settings.get('description'))}\n"
        f"Дата: {_format_value(settings.get('event_datetime'))}\n"
        f"Zoom: {_format_value(settings.get('zoom_link'))}\n"
        f"Оплата: {_format_value(settings.get('payment_link'))}"
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
        "Введите новое название мероприятия (до 200 символов).\n"
        "Доступные команды: Отмена.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_TOPIC


async def set_topic_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()

    if _is_cancel(text):
        await update.message.reply_text("Изменение названия отменено.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    if not text:
        await update.message.reply_text("Название не может быть пустым. Попробуйте снова.")
        return STATE_TOPIC

    if len(text) > TOPIC_MAX_LENGTH:
        await update.message.reply_text(
            f"Название слишком длинное. Максимум {TOPIC_MAX_LENGTH} символов."
        )
        return STATE_TOPIC

    update_settings(topic=text)
    await show_current_event(update, context)
    await update.message.reply_text("✅ Название обновлено.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


async def set_description_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Отправьте новое описание мероприятия.\n"
        "Доступные команды: Отмена, Очистить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_DESCRIPTION


async def set_description_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END

    raw_text = update.message.text or ""
    if _is_cancel(raw_text):
        await update.message.reply_text("Изменение описания отменено.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    if _is_clear(raw_text):
        update_settings(description=None)
        await show_current_event(update, context)
        await update.message.reply_text("Описание очищено.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    stripped = raw_text.strip()
    if not stripped:
        await update.message.reply_text(
            "Описание не может быть пустым. Введите текст или используйте Очистить."
        )
        return STATE_DESCRIPTION

    update_settings(description=stripped)
    await show_current_event(update, context)
    await update.message.reply_text("✅ Описание обновлено.", reply_markup=MENU_KEYBOARD)
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
    ensure_scheduler_started()
    schedule_all_reminders(context.application)
    await show_current_event(update, context)
    await update.message.reply_text("Дата вебинара обновлена.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


async def set_zoom_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Отправьте новую Zoom-ссылку.\nДоступные команды: Отмена, Очистить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_ZOOM


async def set_zoom_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""

    if _is_cancel(text):
        await update.message.reply_text("Обновление Zoom-ссылки отменено.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    if _is_clear(text):
        update_settings(zoom_link=None)
        await show_current_event(update, context)
        await update.message.reply_text("Ссылка Zoom очищена.", reply_markup=MENU_KEYBOARD)
        schedule_all_reminders(context.application)
        return ConversationHandler.END

    link = text.strip()
    if not link or not _is_valid_url(link):
        await update.message.reply_text(
            "Укажите корректную ссылку (http/https) или используйте команды Отмена/Очистить."
        )
        return STATE_ZOOM

    update_settings(zoom_link=link)
    await show_current_event(update, context)
    await update.message.reply_text("Ссылка Zoom обновлена.", reply_markup=MENU_KEYBOARD)
    schedule_all_reminders(context.application)
    return ConversationHandler.END


async def set_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Отправьте ссылку на оплату (http/https).\nДоступные команды: Отмена, Очистить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_PAYMENT


async def set_payment_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""

    if _is_cancel(text):
        await update.message.reply_text("Обновление ссылки на оплату отменено.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    if _is_clear(text):
        update_settings(payment_link=None)
        await show_current_event(update, context)
        await update.message.reply_text("Ссылка на оплату очищена.", reply_markup=MENU_KEYBOARD)
        return ConversationHandler.END

    link = text.strip()
    if not link or not _is_valid_url(link):
        await update.message.reply_text(
            "Укажите корректную ссылку (http/https) или используйте команды Отмена/Очистить."
        )
        return STATE_PAYMENT

    update_settings(payment_link=link)
    await show_current_event(update, context)
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
            MessageHandler(filters.Regex("^✏️"), set_topic_start),
            MessageHandler(filters.Regex("^📝 Изменить описание$"), set_description_start),
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
            STATE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_description_finish)],
        },
        fallbacks=[CommandHandler("cancel", admin_command_entry)],
        allow_reentry=True,
    )
