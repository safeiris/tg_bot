"""Administrative tools for configuring the webinar bot with Google Sheets storage."""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Dict, Optional
from uuid import uuid4

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from unidecode import unidecode
from zoneinfo import ZoneInfo

import database
from config import TIMEZONE, is_admin, load_settings, update_settings
from scheduler import ensure_scheduler_started, schedule_all_reminders

BUTTON_SET_TOPIC = "✏️ Изменить название"
BUTTON_SET_DESCRIPTION = "📝 Изменить описание"
BUTTON_SET_DATE = "📆 Изменить дату"
BUTTON_SET_ZOOM = "🔗 Обновить Zoom"
BUTTON_SET_PAYMENT = "💳 Обновить оплату"
BUTTON_EXPORT = "📥 Список участников"
BUTTON_NOTIFY = "📢 Разослать напоминание"
BUTTON_SHOW_EVENT = "👁 Текущее мероприятие"
BUTTON_NEW_EVENT = "🆕 Новое мероприятие"
BUTTON_CREATE_NEW_PROMPT = "🆕 Создать новое"
BUTTON_CANCEL_ACTION = "Отмена"

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BUTTON_NEW_EVENT, BUTTON_SHOW_EVENT],
        [BUTTON_SET_TOPIC, BUTTON_SET_DESCRIPTION],
        [BUTTON_SET_DATE, BUTTON_SET_ZOOM],
        [BUTTON_SET_PAYMENT, BUTTON_EXPORT],
        [BUTTON_NOTIFY],
    ],
    resize_keyboard=True,
)

(
    STATE_TOPIC,
    STATE_DATE,
    STATE_ZOOM,
    STATE_PAYMENT,
    STATE_NOTIFY,
    STATE_DESCRIPTION,
    STATE_DECIDE_NEW_EVENT,
    STATE_NEW_EVENT_TITLE,
    STATE_NEW_EVENT_DESCRIPTION,
    STATE_NEW_EVENT_DATETIME,
    STATE_NEW_EVENT_ZOOM,
    STATE_NEW_EVENT_PAYMENT,
) = range(12)

CANCEL_TEXT = "отмена"
CLEAR_TEXT = "очистить"
SKIP_TEXT = "пропустить"
TOPIC_MAX_LENGTH = 200
MAX_SLUG_LENGTH = 30

TZ = ZoneInfo(TIMEZONE)
MISSING_VALUE = "❗️Не указано администратором"


def _normalize_command_text(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def _is_cancel(text: Optional[str]) -> bool:
    return _normalize_command_text(text) == CANCEL_TEXT


def _is_clear(text: Optional[str]) -> bool:
    return _normalize_command_text(text) == CLEAR_TEXT


def _is_skip(text: Optional[str]) -> bool:
    return _normalize_command_text(text) == SKIP_TEXT


async def _ensure_admin(update: Update, *, message: str = "Недостаточно прав.") -> bool:
    user = update.effective_user
    if user and is_admin(chat_id=user.id, username=user.username):
        return True
    if update.message:
        await update.message.reply_text(message)
    else:
        await update.effective_chat.send_message(message)
    return False


def _slugify_topic(topic: str) -> str:
    normalized = unidecode(topic or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not normalized:
        normalized = "event"
    return normalized[:MAX_SLUG_LENGTH]


def _generate_sheet_name(topic: str, event_dt: datetime) -> str:
    base_slug = _slugify_topic(topic)
    date_part = event_dt.strftime("%Y-%m-%d")
    candidate = f"{date_part}__{base_slug}"
    suffix = 1
    while database.get_sheet_by_name(candidate) is not None:
        suffix += 1
        trimmed_slug = base_slug[: max(1, MAX_SLUG_LENGTH - len(f"-{suffix}"))]
        candidate = f"{date_part}__{trimmed_slug}-{suffix}"
    return candidate


def _generate_event_id() -> str:
    return uuid4().hex[:12]


def _parse_datetime(text: str) -> datetime:
    text = (text or "").strip()
    formats = ["%d.%m.%Y %H:%M", "%d.%m %H:%M", "%Y-%m-%d %H:%M"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now(TZ).year)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError("invalid format")


def _format_datetime(dt: Optional[datetime]) -> str:
    if not dt:
        return MISSING_VALUE
    local_dt = dt.astimezone(TZ)
    month_names = [
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
    month_name = month_names[local_dt.month] if 1 <= local_dt.month <= 12 else local_dt.strftime("%B")
    return f"{local_dt.day} {month_name} {local_dt.year}, {local_dt.strftime('%H:%M')}"


def _event_datetime(settings: Dict[str, object]) -> Optional[datetime]:
    event_iso = settings.get("current_event_datetime")
    if not event_iso:
        return None
    try:
        return datetime.fromisoformat(str(event_iso))
    except ValueError:
        return None


def _event_has_started(settings: Dict[str, object]) -> bool:
    dt = _event_datetime(settings)
    if not dt:
        return False
    now = datetime.now(TZ)
    return now >= dt


def _format_value(value: Optional[object]) -> str:
    if value is None:
        return MISSING_VALUE
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else MISSING_VALUE
    return str(value)


def _build_event_card(settings: Dict[str, object]) -> str:
    topic = _format_value(settings.get("topic"))
    description = _format_value(settings.get("description"))
    zoom_link = _format_value(settings.get("zoom_link"))
    payment_link = _format_value(settings.get("payment_link"))

    dt = _event_datetime(settings)
    dt_text = _format_datetime(dt)

    lines = ["<b>Текущее мероприятие</b>"]
    lines.append(f"🎓 Название: {html.escape(topic)}")
    lines.append(f"📝 Описание: {html.escape(description)}")
    lines.append(f"📅 Дата и время: {html.escape(dt_text)}")
    lines.append(f"🔗 Zoom: {html.escape(zoom_link)}")
    lines.append(f"💳 Оплата: {html.escape(payment_link)}")
    lines.append(f"🌍 Часовой пояс: {html.escape(TIMEZONE)}")

    sheet_name = settings.get("current_event_sheet_name")
    sheet_gid = settings.get("current_event_sheet_gid")
    if sheet_name:
        try:
            sheet_link = database.get_sheet_link(sheet_name, sheet_gid)
            lines.append(f"📊 Участники: <a href=\"{sheet_link}\">Открыть лист</a>")
        except RuntimeError:
            lines.append("📊 Участники: лист не настроен")
    else:
        lines.append("📊 Участники: лист не настроен")

    if _event_has_started(settings):
        lines.append("\n⚠️ Мероприятие уже началось. Изменения невозможны.")

    return "\n".join(lines)


async def _send_event_card(update: Update, settings: Optional[Dict[str, object]] = None) -> None:
    if settings is None:
        settings = load_settings()
    text = _build_event_card(settings)
    if update.message:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=MENU_KEYBOARD,
            disable_web_page_preview=True,
        )
    else:
        await update.effective_chat.send_message(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=MENU_KEYBOARD,
            disable_web_page_preview=True,
        )


async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_event_card(update)


async def _maybe_require_new_event(update: Update) -> Optional[Dict[str, object]]:
    settings = load_settings()
    if not settings.get("current_event_id") or not settings.get("current_event_sheet_name"):
        await _prompt_new_event_creation(update, started=False)
        return None
    if _event_has_started(settings):
        await _prompt_new_event_creation(update, started=True)
        return None
    return settings


async def _prompt_new_event_creation(update: Update, *, started: bool) -> None:
    keyboard = ReplyKeyboardMarkup(
        [[BUTTON_CREATE_NEW_PROMPT], [BUTTON_CANCEL_ACTION]], resize_keyboard=True
    )
    if started:
        message = (
            "Текущее мероприятие уже началось. Для изменений создайте новое мероприятие."
        )
    else:
        message = "Активное мероприятие не настроено. Создайте новое мероприятие."
    if update.message:
        await update.message.reply_text(message, reply_markup=keyboard)
    else:
        await update.effective_chat.send_message(message, reply_markup=keyboard)


async def show_current_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await _send_event_card(update)
    return ConversationHandler.END


async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update, message="Эта команда доступна только администратору."):
        return ConversationHandler.END
    await send_admin_panel(update, context)
    return ConversationHandler.END


async def set_topic_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    settings = await _maybe_require_new_event(update)
    if settings is None:
        return STATE_DECIDE_NEW_EVENT
    await update.message.reply_text(
        "Введите новое название мероприятия (до 200 символов).\nДоступные команды: Отмена.",
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
    settings = update_settings(topic=text)
    await update.message.reply_text("✅ Название обновлено.", reply_markup=MENU_KEYBOARD)
    await _send_event_card(update, settings)
    return ConversationHandler.END


async def set_description_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    settings = await _maybe_require_new_event(update)
    if settings is None:
        return STATE_DECIDE_NEW_EVENT
    await update.message.reply_text(
        "Отправьте новое описание мероприятия.\nДоступные команды: Отмена, Очистить.",
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
        settings = update_settings(description="")
        await update.message.reply_text("Описание очищено.", reply_markup=MENU_KEYBOARD)
        await _send_event_card(update, settings)
        return ConversationHandler.END
    stripped = raw_text.strip()
    if not stripped:
        await update.message.reply_text(
            "Описание не может быть пустым. Введите текст или используйте Очистить."
        )
        return STATE_DESCRIPTION
    settings = update_settings(description=stripped)
    await update.message.reply_text("✅ Описание обновлено.", reply_markup=MENU_KEYBOARD)
    await _send_event_card(update, settings)
    return ConversationHandler.END


async def set_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    settings = await _maybe_require_new_event(update)
    if settings is None:
        return STATE_DECIDE_NEW_EVENT
    await update.message.reply_text(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ.",
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
    settings = update_settings(current_event_datetime=event_dt.isoformat())
    ensure_scheduler_started()
    schedule_all_reminders(context.application)
    await update.message.reply_text("Дата вебинара обновлена.", reply_markup=MENU_KEYBOARD)
    await _send_event_card(update, settings)
    return ConversationHandler.END


def _is_valid_url(value: str) -> bool:
    pattern = re.compile(r"^https?://.+$", re.IGNORECASE)
    return bool(pattern.match(value))


async def set_zoom_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    settings = await _maybe_require_new_event(update)
    if settings is None:
        return STATE_DECIDE_NEW_EVENT
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
    if _is_clear(text) or _is_skip(text):
        settings = update_settings(zoom_link="")
        await update.message.reply_text("Ссылка Zoom очищена.", reply_markup=MENU_KEYBOARD)
        await _send_event_card(update, settings)
        schedule_all_reminders(context.application)
        return ConversationHandler.END
    link = text.strip()
    if not _is_valid_url(link):
        await update.message.reply_text(
            "Укажите корректную ссылку (http/https) или используйте команды Отмена/Очистить/Пропустить."
        )
        return STATE_ZOOM
    settings = update_settings(zoom_link=link)
    await update.message.reply_text("Ссылка Zoom обновлена.", reply_markup=MENU_KEYBOARD)
    await _send_event_card(update, settings)
    schedule_all_reminders(context.application)
    return ConversationHandler.END


async def set_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    settings = await _maybe_require_new_event(update)
    if settings is None:
        return STATE_DECIDE_NEW_EVENT
    await update.message.reply_text(
        "Отправьте ссылку на оплату (http/https).\nДоступные команды: Отмена, Очистить, Пропустить.",
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
    if _is_clear(text) or _is_skip(text):
        settings = update_settings(payment_link="")
        await update.message.reply_text("Ссылка на оплату очищена.", reply_markup=MENU_KEYBOARD)
        await _send_event_card(update, settings)
        return ConversationHandler.END
    link = text.strip()
    if not _is_valid_url(link):
        await update.message.reply_text(
            "Укажите корректную ссылку (http/https) или используйте команды Отмена/Очистить/Пропустить."
        )
        return STATE_PAYMENT
    settings = update_settings(payment_link=link)
    await update.message.reply_text("Ссылка на оплату обновлена.", reply_markup=MENU_KEYBOARD)
    await _send_event_card(update, settings)
    return ConversationHandler.END


async def export_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    try:
        sheet_link = database.get_sheet_link()
    except RuntimeError:
        await update.message.reply_text(
            "Активный лист не найден. Сначала создайте новое мероприятие.",
            reply_markup=MENU_KEYBOARD,
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"📊 Список участников: {sheet_link}", reply_markup=MENU_KEYBOARD, disable_web_page_preview=True
    )
    path = database.export_database()
    await update.message.reply_document(document=path.read_bytes(), filename=path.name)
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


async def start_new_event_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    context.user_data["new_event_data"] = {}
    await update.message.reply_text(
        "Введите название нового мероприятия.", reply_markup=ReplyKeyboardRemove()
    )
    return STATE_NEW_EVENT_TITLE


async def handle_new_event_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text == BUTTON_CREATE_NEW_PROMPT or text == BUTTON_NEW_EVENT:
        return await start_new_event_flow(update, context)
    await update.message.reply_text("Создание нового мероприятия отменено.", reply_markup=MENU_KEYBOARD)
    return ConversationHandler.END


async def new_event_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if _is_cancel(text):
        await update.message.reply_text("Создание мероприятия отменено.", reply_markup=MENU_KEYBOARD)
        context.user_data.pop("new_event_data", None)
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("Название не может быть пустым. Попробуйте снова.")
        return STATE_NEW_EVENT_TITLE
    context.user_data.setdefault("new_event_data", {})["topic"] = text
    await update.message.reply_text(
        "Отправьте описание мероприятия.\nДоступные команды: Отмена, Пропустить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_NEW_EVENT_DESCRIPTION


async def new_event_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    data = context.user_data.setdefault("new_event_data", {})
    if _is_cancel(text):
        await update.message.reply_text("Создание мероприятия отменено.", reply_markup=MENU_KEYBOARD)
        context.user_data.pop("new_event_data", None)
        return ConversationHandler.END
    if _is_skip(text):
        data["description"] = ""
    else:
        data["description"] = text.strip()
    await update.message.reply_text(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_NEW_EVENT_DATETIME


async def new_event_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    try:
        event_dt = _parse_datetime(text)
    except ValueError:
        await update.message.reply_text("Не удалось распознать дату. Попробуйте снова.")
        return STATE_NEW_EVENT_DATETIME
    context.user_data.setdefault("new_event_data", {})["datetime"] = event_dt
    await update.message.reply_text(
        "Укажите Zoom-ссылку. Доступные команды: Отмена, Пропустить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_NEW_EVENT_ZOOM


async def new_event_zoom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    data = context.user_data.setdefault("new_event_data", {})
    if _is_cancel(text):
        await update.message.reply_text("Создание мероприятия отменено.", reply_markup=MENU_KEYBOARD)
        context.user_data.pop("new_event_data", None)
        return ConversationHandler.END
    if _is_skip(text) or _is_clear(text):
        data["zoom_link"] = ""
    else:
        link = text.strip()
        if not _is_valid_url(link):
            await update.message.reply_text(
                "Укажите корректную ссылку (http/https) или используйте команды Отмена/Пропустить."
            )
            return STATE_NEW_EVENT_ZOOM
        data["zoom_link"] = link
    await update.message.reply_text(
        "Укажите ссылку на оплату. Доступные команды: Отмена, Пропустить.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_NEW_EVENT_PAYMENT


async def _finalize_new_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.get("new_event_data") or {}
    topic = data.get("topic")
    event_dt: Optional[datetime] = data.get("datetime")
    if not topic or not event_dt:
        await update.message.reply_text(
            "Недостаточно данных для создания мероприятия. Попробуйте снова.",
            reply_markup=MENU_KEYBOARD,
        )
        context.user_data.pop("new_event_data", None)
        return ConversationHandler.END

    sheet_name = _generate_sheet_name(topic, event_dt)
    worksheet = database.get_or_create_sheet(sheet_name)
    event_id = _generate_event_id()

    settings = update_settings(
        topic=topic,
        description=data.get("description", ""),
        zoom_link=data.get("zoom_link", ""),
        payment_link=data.get("payment_link", ""),
        current_event_id=event_id,
        current_event_sheet_name=worksheet.title,
        current_event_sheet_gid=worksheet.id,
        current_event_datetime=event_dt.isoformat(),
        timezone=TIMEZONE,
    )

    context.user_data.pop("new_event_data", None)
    ensure_scheduler_started()
    schedule_all_reminders(context.application)

    sheet_link = database.get_sheet_link(worksheet.title, worksheet.id)
    await update.message.reply_text(
        "Новое мероприятие создано ✅\n"
        f"Лист участников: {sheet_link}",
        disable_web_page_preview=True,
        reply_markup=MENU_KEYBOARD,
    )
    await _send_event_card(update, settings)
    return ConversationHandler.END


async def new_event_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = update.message.text or ""
    data = context.user_data.setdefault("new_event_data", {})
    if _is_cancel(text):
        await update.message.reply_text("Создание мероприятия отменено.", reply_markup=MENU_KEYBOARD)
        context.user_data.pop("new_event_data", None)
        return ConversationHandler.END
    if _is_skip(text) or _is_clear(text):
        data["payment_link"] = ""
    else:
        link = text.strip()
        if not _is_valid_url(link):
            await update.message.reply_text(
                "Укажите корректную ссылку (http/https) или используйте команды Отмена/Пропустить."
            )
            return STATE_NEW_EVENT_PAYMENT
        data["payment_link"] = link
    return await _finalize_new_event(update, context)


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
            CommandHandler("new_event", start_new_event_flow),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SET_TOPIC)}$"), set_topic_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SET_DESCRIPTION)}$"), set_description_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SET_DATE)}$"), set_date_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SET_ZOOM)}$"), set_zoom_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SET_PAYMENT)}$"), set_payment_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_EXPORT)}$"), export_participants),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_NOTIFY)}$"), notify_start),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_SHOW_EVENT)}$"), show_current_event),
            MessageHandler(filters.Regex(f"^{re.escape(BUTTON_NEW_EVENT)}$"), start_new_event_flow),
        ],
        states={
            STATE_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_topic_finish)],
            STATE_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_description_finish)
            ],
            STATE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_date_finish)],
            STATE_ZOOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_zoom_finish)],
            STATE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_payment_finish)],
            STATE_NOTIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, notify_finish)],
            STATE_DECIDE_NEW_EVENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_event_decision)
            ],
            STATE_NEW_EVENT_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_title)
            ],
            STATE_NEW_EVENT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_description)
            ],
            STATE_NEW_EVENT_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_datetime)
            ],
            STATE_NEW_EVENT_ZOOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_zoom)
            ],
            STATE_NEW_EVENT_PAYMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_payment)
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_command_entry)],
        allow_reentry=True,
    )

