"""Inline user interaction handlers for the psychology webinar bot."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
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
import notifications
from config import TIMEZONE, is_admin, load_settings
from database import ROLE_FREE, ROLE_PAID, format_role
from events import (
    find_event_id_by_key_persistently,
    get_current_event_id,
    get_event,
    set_current_event,
)
from message_templates import (
    build_free_confirmation,
    build_paid_pending_confirmation,
    get_event_context,
)
from reminders import cancel_user_event_reminders_for_chat, plan_user_event_reminders
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
RESTART_BUTTON_TEXT = "🔄 Начать заново"
RESTART_BUTTON_PATTERN = rf"^{re.escape(RESTART_BUTTON_TEXT)}$"
USER_PAID_CONFIRMED = "user:paid_confirm"

TZ = ZoneInfo(TIMEZONE)
EMAIL_PROMPT_DEDUP_WINDOW = 3.0
RESTART_GUARD_WINDOW = 2.0


@dataclass
class ParticipantStatus:
    registered: bool
    paid: bool
    role: str = ""
    email: str = ""


def _build_restart_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[RESTART_BUTTON_TEXT]], resize_keyboard=True)


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


def _clear_email_prompt_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_email", None)
    context.user_data.pop("email_prompt_ts", None)


def _reset_user_input_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_email_prompt_state(context)
    context.user_data.pop("awaiting_feedback", None)
    context.user_data.pop("pending_registration", None)


def _clear_user_panel_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("last_user_panel_msg_id", None)
    context.user_data.pop("last_user_panel_signature", None)


def _clear_restart_guard(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("restart_in_progress", None)


def _restart_guard_active(context: ContextTypes.DEFAULT_TYPE, now_ts: float) -> bool:
    guard_raw = context.user_data.get("restart_in_progress")
    if isinstance(guard_raw, (int, float)):
        guard_ts = float(guard_raw)
        if now_ts - guard_ts < RESTART_GUARD_WINDOW:
            return True
    _clear_restart_guard(context)
    return False


def _current_ts() -> float:
    return datetime.now(tz=TZ).timestamp()


def _email_prompt_message_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    prompts = context.user_data.get("last_prompts")
    if not isinstance(prompts, dict):
        context.user_data["last_prompts"] = {}
        return None
    message_id = prompts.get("email_prompt_msg_id")
    if isinstance(message_id, int):
        return message_id
    if isinstance(message_id, str) and message_id.isdigit():
        return int(message_id)
    return None


def _store_email_prompt_message_id(
    context: ContextTypes.DEFAULT_TYPE, message_id: int
) -> None:
    prompts = context.user_data.get("last_prompts")
    if not isinstance(prompts, dict):
        prompts = {}
        context.user_data["last_prompts"] = prompts
    prompts["email_prompt_msg_id"] = message_id


async def prompt_user_email(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str = "",
) -> int:
    chat = update.effective_chat
    if chat is None:
        return WAITING_EMAIL

    chat_id = chat.id
    reason = reason or "unknown"
    logger.info("PROMPT_EMAIL start reason=%s", reason)

    user_data = context.user_data
    now_ts = _current_ts()
    awaiting = bool(user_data.get("awaiting_email"))
    last_ts_raw = user_data.get("email_prompt_ts")
    last_ts = float(last_ts_raw) if isinstance(last_ts_raw, (int, float)) else None
    if awaiting and last_ts is not None and now_ts - last_ts < EMAIL_PROMPT_DEDUP_WINDOW:
        logger.info("PROMPT_EMAIL dedup(noop) reason=%s", reason)
        return WAITING_EMAIL

    user_data["awaiting_email"] = True
    user_data["email_prompt_ts"] = now_ts

    message_id = _email_prompt_message_id(context)
    text = "Введи e-mail одним сообщением."
    reply_markup = _build_restart_reply_keyboard()
    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramError:
            message_id = None
        else:
            _store_email_prompt_message_id(context, message_id)
            logger.info("PROMPT_EMAIL edit reason=%s", reason)
            return WAITING_EMAIL

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )
    _store_email_prompt_message_id(context, sent.message_id)
    logger.info("PROMPT_EMAIL send reason=%s", reason)
    return WAITING_EMAIL


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


def _resolve_payment_link(settings: dict) -> str:
    payment_link = str(settings.get("payment_link") or "").strip()
    event_id = settings.get("current_event_id")
    if event_id:
        event = get_event(str(event_id))
        if event:
            event_link = (event.pay_url or "").strip()
            if event_link:
                payment_link = event_link
    if payment_link.startswith("❗️"):
        return ""
    return payment_link


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
        if status.role == ROLE_PAID:
            payment_label = "Оплачено" if status.paid else "Ожидает оплату"
            lines.append(f"💳 Статус оплаты: {payment_label}")
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
    keyboard.append([InlineKeyboardButton(RESTART_BUTTON_TEXT, callback_data=USER_RESTART)])
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
    force_registration: bool = False,
) -> int:
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    chat_id = chat.id
    restart_guard = context.user_data.get("restart_in_progress") if force_registration else None
    if force_registration:
        context.user_data.clear()
        if restart_guard is not None:
            context.user_data["restart_in_progress"] = restart_guard
    _activate_event_payload(context, payload)
    _clear_global_feedback_flag(context, chat_id)
    _reset_user_input_state(context)
    status = _participant_status(chat_id)
    panel_status = status
    status_message: Optional[str] = None
    if force_registration:
        settings_snapshot = load_settings()
        event_id = settings_snapshot.get("current_event_id")
        if status.registered and event_id:
            cancel_user_event_reminders_for_chat(
                context, chat_id=chat_id, event_id=str(event_id)
            )
        if status.registered:
            try:
                database.unregister_participant(chat_id)
            except RuntimeError:
                logger.debug("Failed to unregister participant during reset", exc_info=True)
            status = _participant_status(chat_id)
        panel_status = ParticipantStatus(registered=False, paid=False)
        status_message = "Начнём регистрацию заново."
    _clear_restart_guard(context)
    await _render_user_panel(
        update=update,
        context=context,
        status_obj=panel_status,
        status_message=status_message,
        fresh_panel=fresh_panel,
    )
    _clear_email_prompt_state(context)
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
    force_registration = bool(payload)
    state = await _enter_user_flow(
        update,
        context,
        fresh_panel=True,
        payload=payload,
        force_registration=force_registration,
    )
    return state


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _handle_admin_entry(update, context):
        return ConversationHandler.END
    return await _enter_user_flow(update, context, fresh_panel=True)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _handle_admin_entry(update, context):
        return ConversationHandler.END
    return await _enter_user_flow(update, context, fresh_panel=True, force_registration=True)


async def _handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    status = _participant_status(chat_id)
    if status.registered:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Вы уже зарегистрированы.",
            reply_markup=_build_restart_reply_keyboard(),
        )
        return PANEL
    context.user_data.pop("pending_registration", None)
    return await prompt_user_email(update, context, reason="register_button")



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
    elif data == USER_PAID_CONFIRMED:
        new_state = await _handle_payment_confirmation(update, context)
    _update_conversation_state(update, new_state)
    return new_state


async def _handle_user_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    now_ts = _current_ts()
    if _restart_guard_active(context, now_ts):
        logger.debug("Restart ignored due to in-progress guard")
        return PANEL
    context.user_data["restart_in_progress"] = now_ts
    try:
        return await _enter_user_flow(
            update,
            context,
            fresh_panel=True,
            force_registration=True,
        )
    finally:
        _clear_restart_guard(context)


async def restart_via_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_user_restart(update, context)


async def _handle_payment_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    chat = update.effective_chat
    if chat is None:
        return PANEL
    chat_id = chat.id
    status = _participant_status(chat_id)
    if not status.registered or status.role != ROLE_PAID:
        if query:
            try:
                await query.answer("Сначала зарегистрируйтесь на мероприятие.", show_alert=True)
            except Exception:
                logger.debug("Failed to answer payment callback without registration", exc_info=True)
        return PANEL
    if status.paid:
        if query:
            try:
                await query.answer("Оплата уже подтверждена.")
            except Exception:
                logger.debug("Failed to answer already paid callback", exc_info=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        return PANEL
    settings = load_settings()
    try:
        await notifications.send_paid_confirmation(
            context.bot,
            chat_id,
            settings=settings,
        )
    except Exception:
        logger.exception("Failed to send paid confirmation to %s", chat_id)
        if query:
            try:
                await query.answer("Не удалось подтвердить оплату. Попробуйте позже.", show_alert=True)
            except Exception:
                logger.debug("Failed to answer payment failure callback", exc_info=True)
        return PANEL
    if query:
        try:
            await query.edit_message_text("✅ Оплата отмечена. Спасибо! Ждём вас на встрече.")
        except TelegramError:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        try:
            await query.answer("Оплату зафиксировали 🙌")
        except Exception:
            logger.debug("Failed to answer payment confirmation callback", exc_info=True)
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message="Оплату зафиксировали. До встречи!",
    )
    return PANEL


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = (update.message.text or "").strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text(
            "Кажется, это не похоже на e-mail. Попробуйте ещё раз.",
            reply_markup=_build_restart_reply_keyboard(),
        )
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
    context.user_data.pop("email_prompt_ts", None)
    await update.message.reply_text(
        "E-mail записали ✅",
        reply_markup=_build_restart_reply_keyboard(),
    )
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
        _clear_email_prompt_state(context)
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
        _clear_email_prompt_state(context)
        return PANEL

    await query.edit_message_text(f"✅ Тип участия: {role_label}")
    settings = load_settings()
    payment_markup: Optional[InlineKeyboardMarkup] = None
    if role_label == ROLE_PAID:
        confirmation = build_paid_pending_confirmation(settings)
        payment_link = _resolve_payment_link(settings)
        status_message = "Мы записали ваши данные. Ссылку на оплату отправили сообщением."
        buttons: list[list[InlineKeyboardButton]] = []
        if payment_link:
            buttons.append([
                InlineKeyboardButton("💳 Перейти к оплате", url=payment_link)
            ])
        buttons.append([InlineKeyboardButton("✅ Я оплатил", callback_data=USER_PAID_CONFIRMED)])
        payment_markup = InlineKeyboardMarkup(buttons)
    else:
        confirmation = build_free_confirmation(settings)
        status_message = "Вы успешно зарегистрированы!"

    event_id = settings.get("current_event_id")
    if event_id:
        try:
            plan_user_event_reminders(context, chat_id=chat_id, event_id=str(event_id))
        except Exception:
            logger.exception("Failed to schedule personal reminders for %s", chat_id)

    reply_markup = (
        payment_markup if payment_markup is not None else _build_restart_reply_keyboard()
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=confirmation,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message=status_message,
    )
    context.user_data.pop("pending_registration", None)
    _clear_email_prompt_state(context)
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
        await update.message.reply_text(
            "Напишите, пожалуйста, текст отзыва.",
            reply_markup=_build_restart_reply_keyboard(),
        )
        return WAITING_FEEDBACK
    database.update_feedback(chat_id, feedback)
    awaiting = context.application.bot_data.setdefault("awaiting_feedback", set())
    awaiting.discard(chat_id)
    await update.message.reply_text(
        "Спасибо за обратную связь! 💖",
        reply_markup=_build_restart_reply_keyboard(),
    )
    await _refresh_panel_from_state(
        context=context,
        chat_id=chat_id,
        status_message="Отзыв сохранён.",
    )
    context.user_data.pop("awaiting_feedback", None)
    return PANEL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=_build_restart_reply_keyboard(),
    )
    _clear_email_prompt_state(context)
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
    if feedback == RESTART_BUTTON_TEXT:
        await restart_via_button(update, context)
        return
    if not feedback:
        return
    database.update_feedback(chat_id, feedback)
    awaiting.discard(chat_id)
    await update.message.reply_text(
        "Спасибо за обратную связь! 💖",
        reply_markup=_build_restart_reply_keyboard(),
    )


def build_conversation_handler() -> ConversationHandler:
    global _conversation_handler
    restart_button_filter = filters.Regex(RESTART_BUTTON_PATTERN)
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("menu", menu),
            CommandHandler("reset", reset),
            MessageHandler(restart_button_filter, restart_via_button),
        ],
        states={
            PANEL: [MessageHandler(restart_button_filter, restart_via_button)],
            WAITING_EMAIL: [
                MessageHandler(restart_button_filter, restart_via_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email),
            ],
            WAITING_ROLE: [
                CallbackQueryHandler(handle_role_selection, pattern=rf"^{ROLE_CALLBACK_PREFIX}"),
                MessageHandler(restart_button_filter, restart_via_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_role_text),
            ],
            WAITING_FEEDBACK: [
                MessageHandler(restart_button_filter, restart_via_button),
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
