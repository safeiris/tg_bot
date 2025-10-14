"""Inline user interaction handlers for the psychology webinar bot."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
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
from database import ROLE_FREE, ROLE_PAID, format_role
from events import (
    find_event_id_by_key_persistently,
    get_current_event_id,
    get_event,
    set_current_event,
)
from message_templates import build_free_confirmation, build_paid_pending_confirmation, get_event_context
from reminders import plan_user_event_reminders
from zoneinfo import ZoneInfo

from admin_panel import show_main_menu
from utils import map_event_key, resolve_event_id

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PANEL, WAITING_EMAIL, WAITING_ROLE, WAITING_FEEDBACK = range(4)

ROLE_CALLBACK_PREFIX = "role:"
ROLE_OBSERVER = f"{ROLE_CALLBACK_PREFIX}observer"
ROLE_PARTICIPANT = f"{ROLE_CALLBACK_PREFIX}participant"

_conversation_handler: ConversationHandler | None = None


logger = logging.getLogger(__name__)


async def go_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global handler that always returns the admin panel to the main menu."""

    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            logger.debug("Failed to answer nav:main callback", exc_info=True)

    # Reset any pending input states
    context.user_data.pop("await", None)
    context.user_data.pop("draft_event", None)
    context.user_data.pop("event_wizard_state", None)

    # Close lingering wizard panel if possible
    chat = update.effective_chat
    wizard_message_id = context.user_data.pop("wizard_message_id", None)
    if chat and wizard_message_id:
        try:
            await context.bot.delete_message(
                chat_id=chat.id, message_id=wizard_message_id
            )
        except Exception:
            logger.debug("Failed to delete wizard message", exc_info=True)

    # Ensure navigation stack is reset to the main screen
    stack = context.user_data.setdefault("admin_nav_stack", [])
    if isinstance(stack, list):
        stack.clear()
        stack.append({"screen": "main", "data": {}})
    else:
        context.user_data["admin_nav_stack"] = [{"screen": "main", "data": {}}]

    await show_main_menu(update, context, status_message=None)


def _update_conversation_state(update: Update, new_state: object) -> None:
    if _conversation_handler is None:
        return
    try:
        key = _conversation_handler._get_key(update)  # type: ignore[attr-defined]
    except RuntimeError:
        return
    _conversation_handler._update_state(new_state, key)  # type: ignore[attr-defined]

USER_REGISTER = "user:register"
USER_FEEDBACK = "user:feedback"
USER_RESTART = "user:restart"

TZ = ZoneInfo(TIMEZONE)


@dataclass
class ParticipantStatus:
    registered: bool
    paid: bool
    role: str = ""
    email: str = ""


def _keyboard_signature(markup: InlineKeyboardMarkup) -> tuple[tuple[tuple[str, str | None, str | None], ...], ...]:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return ()
    signature: list[tuple[tuple[str, str | None, str | None], ...]] = []
    for row in markup.inline_keyboard:
        signature.append(tuple((button.text, button.callback_data, button.url) for button in row))
    return tuple(signature)


def _panel_signature(text: str, markup: InlineKeyboardMarkup) -> tuple[str, tuple[tuple[tuple[str, str | None, str | None], ...], ...]]:
    return text, _keyboard_signature(markup)


def _store_panel_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message_id: int,
    signature: tuple[str, tuple[tuple[tuple[str, str | None, str | None], ...], ...]],
) -> None:
    context.user_data["last_user_panel_msg_id"] = message_id
    context.user_data["last_user_panel_signature"] = signature


def _reset_user_input_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_email", None)
    context.user_data.pop("awaiting_feedback", None)
    context.user_data.pop("pending_registration", None)


def _clear_user_panel_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("last_user_panel_msg_id", None)
    context.user_data.pop("last_user_panel_signature", None)


def _clear_global_feedback_flag(
    context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int]
) -> None:
    if chat_id is None:
        return
    application = context.application
    if application is None:
        return
    awaiting = application.bot_data.get("awaiting_feedback")
    if isinstance(awaiting, set):
        awaiting.discard(chat_id)


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
    role_value = format_role(row.get("Тип участия") or "")
    paid_value = (row.get("Статус оплаты") or "").strip().lower()
    paid = paid_value in {"оплачено", "оплатил", "оплатила", "paid", "yes", "да"}
    return ParticipantStatus(
        registered=True,
        paid=paid,
        role=role_value,
        email=(row.get("Email") or "").strip(),
    )


def _build_event_message(
    settings: dict, status: ParticipantStatus, extra: Optional[str] = None
) -> str:
    ctx = get_event_context(settings)
    lines = []
    lines.append(f"🧠 {ctx['title']}")
    lines.append(f"📅 {ctx['local_datetime']} ({ctx['timezone']})")
    lines.append(f"📝 {ctx['description']}")
    lines.append("")
    if status.registered:
        lines.append(f"📧 E-mail: {status.email or '—'}")
        if status.role:
            lines.append(f"👤 Тип участия: {status.role}")
    else:
        lines.append("📧 E-mail: —")
        lines.append("👤 Тип участия: —")
    lines.append("")
    lines.append("Мы пришлём напоминание за 1 день и за 1 час до начала мероприятия.")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def _build_user_keyboard(status: ParticipantStatus) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    if not status.registered:
        keyboard.append([InlineKeyboardButton("✅ Зарегистрироваться", callback_data=USER_REGISTER)])
    keyboard.append([InlineKeyboardButton("📝 Оставить отзыв", callback_data=USER_FEEDBACK)])
    keyboard.append([InlineKeyboardButton("🔄 Начать заново", callback_data=USER_RESTART)])
    return InlineKeyboardMarkup(keyboard)


async def _render_user_panel(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_message: Optional[str] = None,
    status_obj: Optional[ParticipantStatus] = None,
    fresh_panel: bool = False,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    settings = load_settings()
    status = status_obj or _participant_status(chat_id)
    text = _build_event_message(settings, status, status_message)
    keyboard = _build_user_keyboard(status)
    signature = _panel_signature(text, keyboard)
    query = update.callback_query
    message = query.message if (query and not fresh_panel) else None
    stored_id = context.user_data.get("last_user_panel_msg_id")
    if fresh_panel:
        _clear_user_panel_cache(context)
        stored_id = None
    if message and message.chat_id == chat_id:
        if stored_id and stored_id != message.message_id:
            message = None
        else:
            if context.user_data.get("last_user_panel_signature") == signature:
                _store_panel_state(context, message_id=message.message_id, signature=signature)
                return
            try:
                await message.edit_text(
                    text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                _store_panel_state(context, message_id=message.message_id, signature=signature)
                return
            except TelegramError:
                message = None
    if update.message and not message:
        sent = await update.message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        _store_panel_state(context, message_id=sent.message_id, signature=signature)
        return
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _store_panel_state(context, message_id=sent.message_id, signature=signature)


async def _refresh_panel_from_state(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message: Optional[str] = None,
) -> None:
    settings = load_settings()
    status = _participant_status(chat_id)
    text = _build_event_message(settings, status, status_message)
    keyboard = _build_user_keyboard(status)
    signature = _panel_signature(text, keyboard)
    message_id = context.user_data.get("last_user_panel_msg_id")
    if message_id and context.user_data.get("last_user_panel_signature") == signature:
        _store_panel_state(context, message_id=message_id, signature=signature)
        return
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            _store_panel_state(context, message_id=message_id, signature=signature)
            return
        except TelegramError:
            pass
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _store_panel_state(context, message_id=sent.message_id, signature=signature)


def _payload_candidates(payload: str) -> list[str]:
    raw = payload.strip()
    if not raw:
        return []
    variants = {raw}
    for sep in (":", "-", "_"):
        if sep in raw:
            _, tail = raw.split(sep, 1)
            if tail:
                variants.add(tail)
    return [item.strip() for item in variants if item.strip()]


def _activate_event_payload(
    context: ContextTypes.DEFAULT_TYPE, payload: Optional[str]
) -> Optional[str]:
    if not payload:
        return None
    candidates = _payload_candidates(payload)
    if not candidates:
        return None
    for candidate in candidates:
        event_id = resolve_event_id(context, candidate)
        if not event_id:
            event_id = find_event_id_by_key_persistently(candidate)
        if not event_id:
            event = get_event(candidate)
            event_id = event.event_id if event else None
        if not event_id:
            continue
        event = get_event(event_id)
        if event is None:
            continue
        if context.application is not None:
            map_event_key(context, candidate, event_id)
        current = get_current_event_id()
        if current != event_id:
            set_current_event(event_id)
        return event_id
    return None


async def _enter_user_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    fresh_panel: bool,
    payload: Optional[str] = None,
) -> int:
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    chat_id = chat.id
    _activate_event_payload(context, payload)
    _clear_global_feedback_flag(context, chat_id)
    _reset_user_input_state(context)
    status = _participant_status(chat_id)
    await _render_user_panel(
        update=update,
        context=context,
        status_obj=status,
        fresh_panel=fresh_panel,
    )
    if not status.registered:
        context.user_data["awaiting_email"] = True
        await context.bot.send_message(chat_id=chat_id, text="Введи e-mail одним сообщением.")
        return WAITING_EMAIL
    context.user_data.pop("awaiting_email", None)
    return PANEL


async def _handle_admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not (user and is_admin(chat_id=user.id, username=user.username)):
        return False
    import admin_panel

    renderer = getattr(admin_panel, "show_admin_panel", None)
    if renderer is None:
        renderer = getattr(admin_panel, "show_main_menu", None)
    if renderer is None:
        logger.error("Admin panel renderer is unavailable in admin_panel module")
        chat = update.effective_chat
        if chat:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Внутренняя ошибка UI админа…",
            )
        return True
    try:
        await renderer(update, context)
    except Exception:
        logger.exception("Failed to render admin panel during entry command")
        chat = update.effective_chat
        if chat:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Внутренняя ошибка UI админа…",
            )
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _handle_admin_entry(update, context):
        return ConversationHandler.END

    payload = context.args[0] if getattr(context, "args", None) else None
    state = await _enter_user_flow(update, context, fresh_panel=True, payload=payload)
    return state


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _handle_admin_entry(update, context):
        return ConversationHandler.END
    return await _enter_user_flow(update, context, fresh_panel=True)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _handle_admin_entry(update, context):
        return ConversationHandler.END
    return await _enter_user_flow(update, context, fresh_panel=True)


async def _handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    status = _participant_status(chat_id)
    if status.registered:
        await context.bot.send_message(chat_id=chat_id, text="Вы уже зарегистрированы.")
        return PANEL
    context.user_data["awaiting_email"] = True
    context.user_data.pop("pending_registration", None)
    await context.bot.send_message(chat_id=chat_id, text="Введи e-mail одним сообщением.")
    return WAITING_EMAIL



async def _handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["awaiting_feedback"] = True
    await _render_user_panel(
        update=update,
        context=context,
        status_message="Напишите ваш отзыв одним сообщением.",
    )
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    awaiting.add(update.effective_chat.id)
    return WAITING_FEEDBACK


def _role_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ROLE_FREE, callback_data=ROLE_OBSERVER)],
            [InlineKeyboardButton(ROLE_PAID, callback_data=ROLE_PARTICIPANT)],
        ]
    )



async def handle_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            logger.debug("Failed to answer user callback", exc_info=True)
    _reset_user_input_state(context)
    data = query.data if query else ""
    new_state = PANEL
    if data == USER_REGISTER:
        new_state = await _handle_registration(update, context)
    elif data == USER_FEEDBACK:
        new_state = await _handle_feedback(update, context)
    elif data == USER_RESTART:
        new_state = await _handle_user_restart(update, context)
    _update_conversation_state(update, new_state)
    return new_state


async def _handle_user_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _enter_user_flow(update, context, fresh_panel=True)


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = (update.message.text or "").strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Кажется, это не похоже на e-mail. Попробуйте ещё раз.")
        return WAITING_EMAIL

    chat_id = update.effective_chat.id
    user = update.effective_user
    pending = context.user_data.setdefault("pending_registration", {})
    pending.update(
        {
            "email": email,
            "chat_id": chat_id,
            "name": (user.full_name or "") if user else "",
            "username": f"@{user.username}" if user and user.username else "",
        }
    )
    context.user_data["awaiting_email"] = False
    await update.message.reply_text(
        "Выбери тип участия:", reply_markup=_role_selection_keyboard()
    )
    return WAITING_ROLE


async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return WAITING_ROLE
    await query.answer()
    choice = query.data or ""
    if not choice.startswith(ROLE_CALLBACK_PREFIX):
        return WAITING_ROLE

    chat = update.effective_chat
    chat_id = chat.id if chat else context.user_data.get("pending_registration", {}).get("chat_id")
    pending = context.user_data.get("pending_registration") or {}
    email = pending.get("email")
    if chat_id is None or not email:
        await query.edit_message_text("Регистрация устарела. Нажми «Зарегистрироваться» ещё раз.")
        context.user_data.pop("pending_registration", None)
        context.user_data.pop("awaiting_email", None)
        return PANEL

    role_label = ROLE_FREE if choice == ROLE_OBSERVER else ROLE_PAID
    participant = database.Participant(
        name=pending.get("name", ""),
        username=pending.get("username", ""),
        chat_id=chat_id,
        email=email,
        role=role_label,
    )
    try:
        database.register_participant(participant)
    except RuntimeError:
        await query.edit_message_text("Регистрация временно недоступна. Попробуй позже.")
        await _refresh_panel_from_state(
            context=context,
            chat_id=chat_id,
            status_message="Не удалось сохранить регистрацию. Попробуйте позже.",
        )
        context.user_data.pop("pending_registration", None)
        context.user_data.pop("awaiting_email", None)
        return PANEL

    await query.edit_message_text(f"✅ Тип участия: {role_label}")
    settings = load_settings()
    if role_label == ROLE_PAID:
        confirmation = build_paid_pending_confirmation(settings)
        status_message = "Мы записали ваши данные. Ссылку на оплату отправили сообщением."
    else:
        confirmation = build_free_confirmation(settings)
        status_message = "Вы успешно зарегистрированы!"

    event_id = settings.get("current_event_id")
    if event_id:
        try:
            plan_user_event_reminders(context, chat_id=chat_id, event_id=str(event_id))
        except Exception:
            logger.exception("Failed to schedule personal reminders for %s", chat_id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=confirmation,
        disable_web_page_preview=True,
    )
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message=status_message,
    )
    context.user_data.pop("pending_registration", None)
    context.user_data.pop("awaiting_email", None)
    return PANEL


async def handle_role_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Пожалуйста, выбери тип участия кнопкой ниже.",
        reply_markup=_role_selection_keyboard(),
    )
    return WAITING_ROLE


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
    context.user_data.pop("pending_registration", None)
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
    global _conversation_handler
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("menu", menu),
            CommandHandler("reset", reset),
        ],
        states={
            PANEL: [],
            WAITING_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            WAITING_ROLE: [
                CallbackQueryHandler(handle_role_selection, pattern=rf"^{ROLE_CALLBACK_PREFIX}"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_role_text),
            ],
            WAITING_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_feedback_text)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("menu", menu),
            CommandHandler("reset", reset),
        ],
        allow_reentry=True,
    )
    _conversation_handler = conversation
    return conversation
