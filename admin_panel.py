"""Inline administrative tools for configuring the webinar bot."""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Dict, Optional
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo

import database
from config import TIMEZONE, is_admin, load_settings, update_settings
from scheduler import ensure_scheduler_started, schedule_all_reminders

ADMIN_PANEL, WAITING_BROADCAST, WAITING_WELCOME, NEW_EVENT_TITLE, NEW_EVENT_DESCRIPTION, NEW_EVENT_DATETIME, NEW_EVENT_ZOOM, NEW_EVENT_PAYMENT = range(8)

CALLBACK_NEW_EVENT = "admin:new_event"
CALLBACK_VIEW_REG = "admin:registrations"
CALLBACK_BROADCAST = "admin:broadcast"
CALLBACK_WELCOME = "admin:welcome"
CALLBACK_OPEN_SHEET = "admin:sheet"
CALLBACK_EXPORT_CSV = "admin:export"
CALLBACK_REFRESH = "admin:refresh"

TZ = ZoneInfo(TIMEZONE)


def _build_admin_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🆕 Создать мероприятие", callback_data=CALLBACK_NEW_EVENT)],
        [InlineKeyboardButton("📋 Регистрации", callback_data=CALLBACK_VIEW_REG)],
        [InlineKeyboardButton("📢 Напомнить всем", callback_data=CALLBACK_BROADCAST)],
        [InlineKeyboardButton("👋 Приветствие", callback_data=CALLBACK_WELCOME)],
        [InlineKeyboardButton("📊 Google Sheet", callback_data=CALLBACK_OPEN_SHEET)],
        [InlineKeyboardButton("⬇️ Экспорт CSV", callback_data=CALLBACK_EXPORT_CSV)],
        [InlineKeyboardButton("🔄 Обновить", callback_data=CALLBACK_REFRESH)],
    ]
    return InlineKeyboardMarkup(keyboard)


def _format_value(value: Optional[object]) -> str:
    if value is None:
        return "❗️Не указано"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else "❗️Не указано"
    return str(value)


def _event_datetime(settings: Dict[str, object]) -> Optional[datetime]:
    event_iso = settings.get("current_event_datetime")
    if not event_iso:
        return None
    try:
        return datetime.fromisoformat(str(event_iso))
    except ValueError:
        return None


def _format_datetime(dt: Optional[datetime]) -> str:
    if not dt:
        return "❗️Не указано"
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
    month = month_names[local_dt.month] if 1 <= local_dt.month <= 12 else local_dt.strftime("%B")
    return f"{local_dt.day} {month} {local_dt.year}, {local_dt.strftime('%H:%M')}"


def _build_panel_text(settings: Dict[str, object], extra: Optional[str] = None) -> str:
    topic = html.escape(_format_value(settings.get("topic")))
    description = html.escape(_format_value(settings.get("description")))
    welcome = html.escape(_format_value(settings.get("welcome_text")))
    zoom_link = html.escape(settings.get("zoom_link") or "❗️Не указано")
    payment_link = html.escape(settings.get("payment_link") or "❗️Не указано")
    dt_text = html.escape(_format_datetime(_event_datetime(settings)))

    lines = ["<b>Панель администратора</b>"]
    lines.append(f"👋 Приветствие: {welcome}")
    lines.append(f"🎓 Название: {topic}")
    lines.append(f"📝 Описание: {description}")
    lines.append(f"📅 Дата и время: {dt_text}")
    lines.append(f"🔗 Zoom: {zoom_link}")
    lines.append(f"💳 Оплата: {payment_link}")

    sheet_name = settings.get("current_event_sheet_name")
    if sheet_name:
        lines.append(f"📊 Активный лист: {html.escape(sheet_name)}")
    else:
        lines.append("📊 Активный лист: не настроен")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


async def _ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if user and is_admin(chat_id=user.id, username=user.username):
        return True
    if update.message:
        await update.message.reply_text("Недостаточно прав для выполнения команды.")
    elif update.callback_query:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
    return False


async def show_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    settings = load_settings()
    text = _build_panel_text(settings, status_message)
    keyboard = _build_admin_keyboard()
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        context.user_data["admin_panel_message_id"] = update.callback_query.message.message_id
        return

    message_id = context.user_data.get("admin_panel_message_id")
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    if chat_id:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        context.user_data["admin_panel_message_id"] = sent.message_id


async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await show_admin_panel(update, context)
    context.user_data.pop("new_event", None)
    return ADMIN_PANEL


def _slugify_topic(topic: str) -> str:
    normalized = re.sub(r"[^\w\s-]", "", topic, flags=re.UNICODE)
    normalized = normalized.strip().lower()
    normalized = re.sub(r"[\s./]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "event"


def _generate_sheet_name(topic: str, event_dt: datetime) -> str:
    base_slug = _slugify_topic(topic)
    date_part = event_dt.astimezone(TZ).strftime("%d-%m-%Y")
    candidate = f"{date_part}__{base_slug}"
    suffix = 1
    while database.get_sheet_by_name(candidate) is not None:
        suffix += 1
        candidate = f"{date_part}__{base_slug}-{suffix}"
    return candidate[:30]


def _generate_event_id() -> str:
    return uuid4().hex[:12]


def _parse_datetime(text: str) -> datetime:
    variants = ["%d.%m.%Y %H:%M", "%d.%m %H:%M", "%Y-%m-%d %H:%M"]
    for fmt in variants:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now(TZ).year)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError("invalid datetime")


async def _handle_view_reg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        df = database.get_participants()
    except RuntimeError:
        await show_admin_panel(
            update,
            context,
            status_message="Активное мероприятие не настроено. Создайте новое событие.",
        )
        return ADMIN_PANEL
    total = len(df.index)
    preview = "\n".join(
        f"• {row.get('Имя') or row.get('Имя пользователя') or row.get('Email') or '—'}"
        for _, row in df.head(10).iterrows()
    )
    extra = f"Регистраций: {total}"
    if preview:
        extra += f"\nПервые записи:\n{preview}"
    await show_admin_panel(update, context, status_message=extra)
    return ADMIN_PANEL


async def _handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    await show_admin_panel(
        update,
        context,
        status_message="Введите текст напоминания, который получат все участники.",
    )
    return WAITING_BROADCAST


async def _handle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    await show_admin_panel(
        update,
        context,
        status_message="Отправьте новый текст приветствия для участников.",
    )
    return WAITING_WELCOME


async def _handle_open_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        link = database.get_sheet_link()
    except RuntimeError:
        await show_admin_panel(
            update,
            context,
            status_message="Активный лист не настроен. Создайте мероприятие.",
        )
        return ADMIN_PANEL
    await update.effective_chat.send_message(link, disable_web_page_preview=False)
    await show_admin_panel(
        update,
        context,
        status_message="Ссылка на Google Sheet отправлена в чат.",
    )
    return ADMIN_PANEL


async def _handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        path = database.export_database_csv()
    except RuntimeError:
        await show_admin_panel(
            update,
            context,
            status_message="Не удалось экспортировать: активное мероприятие не настроено.",
        )
        return ADMIN_PANEL
    await update.effective_chat.send_document(document=path.read_bytes(), filename=path.name)
    await show_admin_panel(
        update,
        context,
        status_message="CSV-файл со списком участников отправлен.",
    )
    return ADMIN_PANEL


async def _handle_new_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["new_event"] = {}
    await show_admin_panel(
        update,
        context,
        status_message="Введите название нового мероприятия.",
    )
    return NEW_EVENT_TITLE


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ADMIN_PANEL
    if not await _ensure_admin(update):
        await query.answer()
        return ConversationHandler.END
    data = query.data
    if data == CALLBACK_NEW_EVENT:
        return await _handle_new_event(update, context)
    if data == CALLBACK_VIEW_REG:
        return await _handle_view_reg(update, context)
    if data == CALLBACK_BROADCAST:
        return await _handle_broadcast(update, context)
    if data == CALLBACK_WELCOME:
        return await _handle_welcome(update, context)
    if data == CALLBACK_OPEN_SHEET:
        return await _handle_open_sheet(update, context)
    if data == CALLBACK_EXPORT_CSV:
        return await _handle_export(update, context)
    if data == CALLBACK_REFRESH:
        await show_admin_panel(update, context)
        return ADMIN_PANEL
    await query.answer()
    return ADMIN_PANEL


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Сообщение не может быть пустым. Попробуйте снова.")
        return WAITING_BROADCAST
    participants = database.list_chat_ids()
    for chat_id in participants:
        await context.bot.send_message(chat_id=chat_id, text=text)
    await show_admin_panel(update, context, status_message="Рассылка успешно отправлена.")
    return ADMIN_PANEL


async def handle_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Приветствие не может быть пустым.")
        return WAITING_WELCOME
    settings = update_settings(welcome_text=text)
    await show_admin_panel(update, context, status_message="Приветствие обновлено.")
    return ADMIN_PANEL


async def new_event_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Название не может быть пустым. Попробуйте снова.")
        return NEW_EVENT_TITLE
    context.user_data.setdefault("new_event", {})["topic"] = text
    await show_admin_panel(update, context, status_message="Введите описание мероприятия (можно пропустить пустым сообщением).")
    return NEW_EVENT_DESCRIPTION


async def new_event_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    context.user_data.setdefault("new_event", {})["description"] = text
    await show_admin_panel(
        update,
        context,
        status_message="Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ.",
    )
    return NEW_EVENT_DATETIME


async def new_event_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        dt = _parse_datetime(text)
    except ValueError:
        await update.message.reply_text("Не удалось распознать дату. Попробуйте снова.")
        return NEW_EVENT_DATETIME
    context.user_data.setdefault("new_event", {})["datetime"] = dt
    await show_admin_panel(update, context, status_message="Укажите ссылку на Zoom (или отправьте пустое сообщение).")
    return NEW_EVENT_ZOOM


async def new_event_zoom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    context.user_data.setdefault("new_event", {})["zoom_link"] = text
    await show_admin_panel(update, context, status_message="Укажите ссылку на оплату (или отправьте пустое сообщение).")
    return NEW_EVENT_PAYMENT


async def _finalize_new_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.get("new_event") or {}
    topic = data.get("topic")
    event_dt: Optional[datetime] = data.get("datetime")
    if not topic or not event_dt:
        await show_admin_panel(
            update,
            context,
            status_message="Недостаточно данных для создания мероприятия.",
        )
        context.user_data.pop("new_event", None)
        return ADMIN_PANEL
    description = data.get("description", "")
    zoom_link = data.get("zoom_link", "")
    payment_link = data.get("payment_link", "")

    sheet_name = _generate_sheet_name(topic, event_dt)
    worksheet = database.get_or_create_sheet(sheet_name)
    event_id = _generate_event_id()

    update_settings(
        topic=topic,
        description=description,
        zoom_link=zoom_link,
        payment_link=payment_link,
        current_event_id=event_id,
        current_event_sheet_name=worksheet.title,
        current_event_sheet_gid=worksheet.id,
        current_event_datetime=event_dt.isoformat(),
        timezone=TIMEZONE,
    )

    context.user_data.pop("new_event", None)
    ensure_scheduler_started()
    schedule_all_reminders(context.application)

    link = database.get_sheet_link(worksheet.title, worksheet.id)
    await show_admin_panel(
        update,
        context,
        status_message=f"Новое мероприятие создано. Ссылка на лист: {link}",
    )
    return ADMIN_PANEL


async def new_event_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    context.user_data.setdefault("new_event", {})["payment_link"] = text
    return await _finalize_new_event(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _ensure_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Действие отменено.")
    context.user_data.pop("new_event", None)
    await show_admin_panel(update, context)
    return ADMIN_PANEL


def build_admin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_command_entry)],
        states={
            ADMIN_PANEL: [CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:")],
            WAITING_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_text)],
            WAITING_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_welcome_text)],
            NEW_EVENT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_title)],
            NEW_EVENT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_description)
            ],
            NEW_EVENT_DATETIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_datetime)
            ],
            NEW_EVENT_ZOOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_zoom)],
            NEW_EVENT_PAYMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_event_payment)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
