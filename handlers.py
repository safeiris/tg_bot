"""Inline user interaction handlers for the psychology webinar bot."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database
from config import TIMEZONE, is_admin, load_settings
from message_templates import build_free_confirmation, get_event_context
from notifications import send_paid_confirmation
from reminders import cancel_personal_reminder, schedule_personal_reminder
from zoneinfo import ZoneInfo

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PANEL, WAITING_EMAIL, WAITING_FEEDBACK = range(3)

USER_REGISTER = "user:register"
USER_REMIND_HOUR = "user:remind:hour"
USER_REMIND_DAY = "user:remind:day"
USER_UNSUBSCRIBE = "user:unsubscribe"
USER_CONFIRMED_PAYMENT = "user:paid"
USER_FEEDBACK = "user:feedback"
USER_LOCATION = "user:location"
USER_CALENDAR = "user:calendar"

REMINDER_HOUR = "hour"
REMINDER_DAY = "day"
TZ = ZoneInfo(TIMEZONE)


@dataclass
class ParticipantStatus:
    registered: bool
    paid: bool
    role: str = ""
    email: str = ""


def _get_event_datetime(settings: Optional[dict] = None) -> Optional[datetime]:
    if settings is None:
        settings = load_settings()
    event_iso = settings.get("current_event_datetime")
    if not event_iso:
        return None
    try:
        return datetime.fromisoformat(str(event_iso))
    except ValueError:
        return None


def _participant_status(chat_id: int) -> ParticipantStatus:
    row = database.get_participant(chat_id)
    if not row:
        return ParticipantStatus(registered=False, paid=False)
    role_value = (row.get("Тип участия") or "").strip().lower()
    paid_value = (row.get("Статус оплаты") or "").strip().lower()
    paid = paid_value in {"оплачено", "оплатил", "оплатила", "paid", "yes", "да"}
    return ParticipantStatus(
        registered=True,
        paid=paid,
        role=role_value,
        email=(row.get("Email") or "").strip(),
    )


def _build_status_text(status: ParticipantStatus) -> str:
    if not status.registered:
        return "🟡 Статус: вы ещё не зарегистрированы."
    if status.paid:
        return "🟢 Статус: участие подтверждено (оплачено)."
    return "🟠 Статус: регистрация получена, ожидаем оплату."


def _build_event_message(settings: dict, status: ParticipantStatus, extra: Optional[str] = None) -> str:
    ctx = get_event_context(settings)
    welcome = (settings.get("welcome_text") or "").strip()
    lines = []
    if welcome:
        lines.append(welcome)
    lines.append(f"🧠 Мероприятие: {ctx['title']}")
    lines.append(f"📝 {ctx['description']}")
    lines.append(f"📅 {ctx['local_datetime']} ({ctx['timezone']})")
    zoom_link = settings.get("zoom_link") or ""
    if zoom_link:
        lines.append(f"🔗 Zoom: {zoom_link}")
    payment = settings.get("payment_link") or ""
    if payment:
        lines.append(f"💳 Оплата: {payment}")
    lines.append("")
    lines.append(_build_status_text(status))
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def _build_user_keyboard(status: ParticipantStatus) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    if not status.registered:
        keyboard.append([InlineKeyboardButton("✅ Зарегистрироваться", callback_data=USER_REGISTER)])
    else:
        keyboard.append(
            [
                InlineKeyboardButton("🔔 Напомнить за 1 час", callback_data=USER_REMIND_HOUR),
                InlineKeyboardButton("⏰ Напомнить за 1 день", callback_data=USER_REMIND_DAY),
            ]
        )
        keyboard.append([InlineKeyboardButton("❌ Отписаться", callback_data=USER_UNSUBSCRIBE)])
        if not status.paid:
            keyboard.append([InlineKeyboardButton("💳 Я оплатил(а)", callback_data=USER_CONFIRMED_PAYMENT)])
    keyboard.append([InlineKeyboardButton("📝 Оставить отзыв", callback_data=USER_FEEDBACK)])
    keyboard.append([InlineKeyboardButton("📍 Локация/ссылка", callback_data=USER_LOCATION)])
    keyboard.append([InlineKeyboardButton("🗓 Добавить в календарь", callback_data=USER_CALENDAR)])
    return InlineKeyboardMarkup(keyboard)


async def _render_user_panel(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_message: Optional[str] = None,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    settings = load_settings()
    status = _participant_status(chat_id)
    text = _build_event_message(settings, status, status_message)
    keyboard = _build_user_keyboard(status)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        context.user_data["panel_message_id"] = update.callback_query.message.message_id
    elif update.message:
        sent = await update.message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        context.user_data["panel_message_id"] = sent.message_id
    else:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        context.user_data["panel_message_id"] = sent.message_id


async def _refresh_panel_from_state(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message: Optional[str] = None,
) -> None:
    message_id = context.user_data.get("panel_message_id")
    settings = load_settings()
    status = _participant_status(chat_id)
    text = _build_event_message(settings, status, status_message)
    keyboard = _build_user_keyboard(status)
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user and is_admin(chat_id=user.id, username=user.username):
        from admin_panel import show_admin_panel

        await show_admin_panel(update, context)
        return ConversationHandler.END

    await _render_user_panel(update=update, context=context)
    context.user_data.pop("awaiting_email", None)
    context.user_data.pop("awaiting_feedback", None)
    return PANEL


async def _handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    chat_id = update.effective_chat.id
    status = _participant_status(chat_id)
    if status.registered:
        await _render_user_panel(update=update, context=context, status_message="Вы уже зарегистрированы.")
        return PANEL
    context.user_data["awaiting_email"] = True
    await _render_user_panel(
        update=update,
        context=context,
        status_message="Отправьте, пожалуйста, ваш e-mail одним сообщением.",
    )
    return WAITING_EMAIL


async def _handle_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    chat_id = update.effective_chat.id
    status = _participant_status(chat_id)
    if not status.registered:
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Сначала зарегистрируйтесь, чтобы настраивать напоминания.",
        )
        return PANEL

    settings = load_settings()
    event_dt = _get_event_datetime(settings)
    if not event_dt:
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Дата мероприятия пока не указана. Мы напомним позже автоматически.",
        )
        return PANEL

    if label == REMINDER_DAY:
        run_at = event_dt - timedelta(days=1)
        message = "Напоминаем: до начала мероприятия остался один день!"
    else:
        run_at = event_dt - timedelta(hours=1)
        message = "Через час начинается мероприятие. До встречи!"

    scheduled = schedule_personal_reminder(
        context,
        chat_id=chat_id,
        run_at=run_at,
        message=message,
        label=label,
    )
    if not scheduled:
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Это напоминание уже неактуально, время прошло.",
        )
        return PANEL
    local_dt = scheduled.astimezone(TZ)
    await _render_user_panel(
        update=update,
        context=context,
        status_message=f"Личное напоминание настроено на {local_dt.strftime('%d.%m %H:%M')}.",
    )
    return PANEL


async def _handle_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    chat_id = update.effective_chat.id
    if database.unregister_participant(chat_id):
        cancel_personal_reminder(context, chat_id, REMINDER_DAY)
        cancel_personal_reminder(context, chat_id, REMINDER_HOUR)
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Вы успешно отписались от участия.",
        )
    else:
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Активной регистрации не найдено.",
        )
    return PANEL


async def _handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    chat_id = update.effective_chat.id
    status = _participant_status(chat_id)
    if not status.registered:
        await _render_user_panel(
            update=update,
            context=context,
            status_message="Сначала зарегистрируйтесь, затем подтвердите оплату.",
        )
        return PANEL

    settings = load_settings()
    await send_paid_confirmation(context.bot, chat_id, settings=settings)
    await _render_user_panel(
        update=update,
        context=context,
        status_message="Спасибо! Мы отметили оплату и отправили подтверждение.",
    )
    return PANEL


async def _handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data["awaiting_feedback"] = True
    await _render_user_panel(
        update=update,
        context=context,
        status_message="Напишите ваш отзыв одним сообщением.",
    )
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    awaiting.add(update.effective_chat.id)
    return WAITING_FEEDBACK


async def _handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    settings = load_settings()
    location_link = settings.get("zoom_link") or settings.get("location_link")
    if location_link:
        await update.effective_chat.send_message(
            f"Локация/ссылка: {location_link}", disable_web_page_preview=False
        )
    else:
        await update.effective_chat.send_message("Локация будет объявлена позже.")
    await _render_user_panel(update=update, context=context)
    return PANEL


def _build_ics_content(settings: dict) -> Optional[str]:
    event_dt = _get_event_datetime(settings)
    if not event_dt:
        return None
    ctx = get_event_context(settings)
    dt_start = event_dt.astimezone(TZ)
    dt_end = dt_start + timedelta(hours=1)
    def _format(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")

    ics = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Psychology Webinar//EN",
        "BEGIN:VEVENT",
        f"UID:{dt_start.strftime('%Y%m%dT%H%M%S')}@psychology-webinar",
        f"DTSTART;TZID={TIMEZONE}:{_format(dt_start)}",
        f"DTEND;TZID={TIMEZONE}:{_format(dt_end)}",
        f"SUMMARY:{ctx['title']}",
        f"DESCRIPTION:{ctx['description']}",
    ]
    location = settings.get("zoom_link") or settings.get("location_link")
    if location:
        ics.append(f"LOCATION:{location}")
    ics.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\n".join(ics)


async def _handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    settings = load_settings()
    content = _build_ics_content(settings)
    if not content:
        await update.effective_chat.send_message(
            "Дата мероприятия пока не настроена, календарь недоступен."
        )
    else:
        await update.effective_chat.send_document(
            document=content.encode("utf-8"),
            filename="event.ics",
            caption="Добавьте событие в ваш календарь",
        )
    await _render_user_panel(update=update, context=context)
    return PANEL


async def handle_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = update.callback_query.data if update.callback_query else ""
    if data == USER_REGISTER:
        return await _handle_registration(update, context)
    if data == USER_REMIND_DAY:
        return await _handle_reminder(update, context, REMINDER_DAY)
    if data == USER_REMIND_HOUR:
        return await _handle_reminder(update, context, REMINDER_HOUR)
    if data == USER_UNSUBSCRIBE:
        return await _handle_unsubscribe(update, context)
    if data == USER_CONFIRMED_PAYMENT:
        return await _handle_payment_confirmation(update, context)
    if data == USER_FEEDBACK:
        return await _handle_feedback(update, context)
    if data == USER_LOCATION:
        return await _handle_location(update, context)
    if data == USER_CALENDAR:
        return await _handle_calendar(update, context)
    await update.callback_query.answer()
    return PANEL


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = (update.message.text or "").strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Кажется, это не похоже на e-mail. Попробуйте ещё раз.")
        return WAITING_EMAIL

    chat_id = update.effective_chat.id
    user = update.effective_user
    participant = database.Participant(
        name=(user.full_name or "") if user else "",
        username=f"@{user.username}" if user and user.username else "",
        chat_id=chat_id,
        email=email,
    )
    try:
        database.register_participant(participant)
    except RuntimeError:
        await update.message.reply_text(
            "Регистрация временно недоступна. Попробуйте позже."
        )
        await _refresh_panel_from_state(
            context=context,
            chat_id=chat_id,
            status_message="Не удалось зарегистрироваться. Попробуйте позже.",
        )
        return PANEL

    settings = load_settings()
    confirmation = build_free_confirmation(settings)
    await update.message.reply_text(confirmation)
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message="Вы успешно зарегистрированы!",
    )
    context.user_data.pop("awaiting_email", None)
    return PANEL


async def handle_feedback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    feedback = (update.message.text or "").strip()
    if not feedback:
        await update.message.reply_text("Напишите, пожалуйста, текст отзыва.")
        return WAITING_FEEDBACK
    database.update_feedback(chat_id, feedback)
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    awaiting.discard(chat_id)
    await update.message.reply_text("Спасибо за обратную связь! 💖")
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message="Отзыв сохранён.",
    )
    context.user_data.pop("awaiting_feedback", None)
    return PANEL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Действие отменено.")
    context.user_data.pop("awaiting_email", None)
    context.user_data.pop("awaiting_feedback", None)
    await _refresh_panel_from_state(context=context, chat_id=update.effective_chat.id)
    return PANEL


async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    if chat_id not in awaiting:
        return
    feedback = (update.message.text or "").strip()
    if not feedback:
        return
    database.update_feedback(chat_id, feedback)
    awaiting.discard(chat_id)
    await update.message.reply_text("Спасибо за обратную связь! 💖")


def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PANEL: [CallbackQueryHandler(handle_user_callback, pattern=r"^user:")],
            WAITING_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            WAITING_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_feedback_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
