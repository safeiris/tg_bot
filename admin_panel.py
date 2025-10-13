"""Admin inline interface with hierarchical navigation."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from zoneinfo import ZoneInfo

import database
from config import TIMEZONE, is_admin
from events import (
    Event,
    classify_status,
    create_event,
    events_bootstrap,
    events_refresh_if_stale,
    get_active_event,
    get_current_event,
    get_current_event_id,
    get_event,
    get_events_page,
    has_active_event,
    open_sheet_url,
    set_current_event,
    update_event,
)
from scheduler import ensure_scheduler_started, schedule_all_reminders

TZ = ZoneInfo(TIMEZONE)
PAGE_SIZE = 5
WIZARD_STEP_TITLE = "title"
WIZARD_STEP_DATETIME = "datetime"
WIZARD_STEP_ZOOM = "zoom"
WIZARD_STEP_PAY = "pay"
WIZARD_STEP_READY = "ready"

ACTIVE_EVENT_WARNING = (
    "⚠️ Пока есть активное мероприятие.\n"
    "Дождись окончания текущего, чтобы создать новое 💗"
)

logger = logging.getLogger(__name__)


def _add_home_button(rows: List[List[InlineKeyboardButton]]) -> List[List[InlineKeyboardButton]]:
    extended = [list(row) for row in rows]
    extended.append([InlineKeyboardButton("🏠 В главное меню", callback_data="nav:main")])
    return extended


def _stack(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, object]]:
    return context.user_data.setdefault("admin_nav_stack", [])


def _reset_stack(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_nav_stack"] = []


def _current_entry(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, object]]:
    stack = _stack(context)
    return stack[-1] if stack else None


def _push_entry(context: ContextTypes.DEFAULT_TYPE, screen: str, **data: object) -> None:
    _stack(context).append({"screen": screen, "data": data})


def _replace_top(context: ContextTypes.DEFAULT_TYPE, screen: str, **data: object) -> None:
    stack = _stack(context)
    if stack:
        stack[-1] = {"screen": screen, "data": data}
    else:
        _push_entry(context, screen, **data)


def _pop_entry(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, object]]:
    stack = _stack(context)
    if not stack:
        return None
    return stack.pop()


def _clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft_event", None)
    context.user_data.pop("event_wizard_state", None)


def _clear_await(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("await", None)


async def _ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if user and is_admin(chat_id=user.id, username=user.username):
        return True
    if update.callback_query:
        await update.callback_query.answer("Недостаточно прав", show_alert=True)
    elif update.message:
        await update.message.reply_text("Недостаточно прав для выполнения команды.")
    return False


async def admin_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return
    _clear_draft(context)
    _clear_await(context)
    await _close_wizard_panel(update, context)
    _reset_stack(context)
    _push_entry(context, "main")
    await show_main_menu(update, context)


async def _send_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    message_id = context.user_data.get("admin_panel_message_id")
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        context.user_data["admin_panel_message_id"] = update.callback_query.message.message_id
        return
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
    if update.message:
        sent = await update.message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        context.user_data["admin_panel_message_id"] = sent.message_id
    elif chat_id:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        context.user_data["admin_panel_message_id"] = sent.message_id


async def _send_wizard_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    message_id = context.user_data.get("wizard_message_id")
    if message_id:
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
            logger.debug("Failed to edit wizard message, sending a new one", exc_info=True)
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.user_data["wizard_message_id"] = sent.message_id


async def _close_wizard_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message_id = context.user_data.pop("wizard_message_id", None)
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Визард закрыт.",
            )
        except Exception:
            logger.debug("Unable to close wizard message gracefully", exc_info=True)


def _format_event_datetime(event: Event) -> str:
    dt = event.parsed_datetime
    if not dt:
        return "❗️Не указано"
    local = dt.astimezone(ZoneInfo(event.timezone or TIMEZONE))
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
    if 1 <= local.month < len(month_names):
        month = month_names[local.month]
    else:
        month = local.strftime("%B")
    return f"{local.day} {month} {local.year}, {local.strftime('%H:%M')}"


def _format_event_card(event: Optional[Event], status_message: Optional[str] = None) -> str:
    lines: List[str] = ["<b>Админ-панель</b>"]
    if event:
        status = classify_status(event)
        status_label = {
            "active": "🟢 Активно",
            "past": "🔵 Прошло",
            "cancelled": "🔴 Отменено",
        }.get(status, status)
        lines.append(f"Статус: {status_label}")
        lines.append(f"🧠 Название: {html.escape(event.title or '—')}")
        description = event.description or "—"
        lines.append(f"📝 Описание: {html.escape(description)}")
        lines.append(f"📅 Дата и время: {html.escape(_format_event_datetime(event))}")
        zoom = html.escape(event.zoom_url or "—")
        lines.append(f"🔗 Zoom: {zoom}")
        payment = html.escape(event.pay_url or "—")
        lines.append(f"💳 Оплата: {payment}")
        lines.append(f"🌍 Часовой пояс: {html.escape(event.timezone or TIMEZONE)}")
        lines.append(f"📄 Лист: {html.escape(event.sheet_name or '—')}")
    else:
        lines.append("⚠️ Активное мероприятие не выбрано.")
        lines.append("Создайте новое или выберите из списка.")
    if status_message:
        lines.append("")
        lines.append(status_message)
    return "\n".join(lines)


def _main_menu_keyboard(active_event: Optional[Event]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if active_event:
        rows.append([InlineKeyboardButton("🛠 Управление текущим", callback_data="admin:menu:manage")])
    else:
        rows.append([InlineKeyboardButton("🆕 Новое мероприятие", callback_data="admin:menu:new")])
    rows.append([InlineKeyboardButton("📅 Мои мероприятия", callback_data="admin:menu:list")])
    rows.append([InlineKeyboardButton("📄 Просмотр участников", callback_data="admin:menu:participants")])
    rows.append([InlineKeyboardButton("📣 Напомнить всем", callback_data="admin:menu:remind")])
    return InlineKeyboardMarkup(_add_home_button(rows))


async def _show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    active_event = get_active_event()
    event = active_event or get_current_event()
    text = _format_event_card(event, status_message)
    await _send_panel(update, context, text, _main_menu_keyboard(active_event))


async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    """Public entry point to render the admin main menu."""
    await _show_main_menu(update, context, status_message=status_message)


async def show_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    """Backward-compatible alias for :func:`show_main_menu`."""
    await show_main_menu(update, context, status_message=status_message)


def _list_row(event: Event) -> str:
    dt = _format_event_datetime(event)
    status = classify_status(event)
    status_map = {
        "active": "",
        "past": " (прошло)",
        "cancelled": " (отменено)",
    }
    suffix = status_map.get(status, "")
    return f"{html.escape(event.title or 'Без названия')} — {html.escape(dt)}{suffix}"


def _list_keyboard(
    events: List[Event],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if events:
        for event in events:
            rows.append(
                [
                    InlineKeyboardButton(
                        event.event_id, callback_data=f"admin:list:pick:{event.event_id}"
                    )
                ]
            )
    else:
        rows.append([InlineKeyboardButton("🆕 Новое", callback_data="admin:menu:new")])
    if events and total_pages > 1:
        prev_page = max(1, page - 1)
        next_page = min(total_pages, page + 1)
        rows.append(
            [
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data=(
                        f"admin:list:page:{prev_page}" if page > 1 else f"admin:list:page:{page}"
                    ),
                ),
                InlineKeyboardButton(
                    f"Стр. {page}/{total_pages}", callback_data="admin:list:page:noop"
                ),
                InlineKeyboardButton(
                    "Вперёд ▶️",
                    callback_data=(
                        f"admin:list:page:{next_page}" if page < total_pages else f"admin:list:page:{page}"
                    ),
                ),
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:list:back")])
    return InlineKeyboardMarkup(_add_home_button(rows))


async def _show_event_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 1,
    status_message: Optional[str] = None,
) -> None:
    bot_data = context.application.bot_data if context.application else None
    events_refresh_if_stale(bot_data=bot_data)
    events, total_pages, total, actual_page = get_events_page(
        page, PAGE_SIZE, bot_data=bot_data
    )
    if events:
        lines = ["📅 Ваши мероприятия"]
        for idx, event in enumerate(events, start=1):
            lines.append(f"{idx}) {_list_row(event)}")
    else:
        lines = ["📅 Ваши мероприятия", "Пока событий нет. Создайте новое."]
    if status_message:
        lines.append("")
        lines.append(status_message)
    text = "\n".join(lines)
    keyboard = _list_keyboard(events, actual_page, total_pages)
    _replace_top(context, "list", page=actual_page)
    await _send_panel(update, context, text, keyboard)


def _draft(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, object]:
    draft = context.user_data.setdefault(
        "draft_event",
        {
            "title": "",
            "datetime": None,
            "timezone": TIMEZONE,
            "zoom_url": "",
            "pay_url": "",
        },
    )
    if not draft.get("timezone"):
        draft["timezone"] = TIMEZONE
    return draft


def _format_draft_datetime(draft: Dict[str, object]) -> str:
    dt: Optional[datetime] = draft.get("datetime")
    if not dt:
        return "❗️Не указано"
    tz = draft.get("timezone") or TIMEZONE
    formatted = _format_event_datetime(
        Event(
            event_id="draft",
            title="",
            description="",
            datetime_local=dt.astimezone(ZoneInfo(tz)).isoformat(),
            timezone=str(tz),
            zoom_url="",
            pay_url="",
            sheet_name="",
            sheet_link="",
            status="active",
            created_at="",
            updated_at="",
        )
    )
    return f"{formatted} ({tz})"


def _wizard_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, object]:
    state = context.user_data.setdefault(
        "event_wizard_state", {"step": WIZARD_STEP_TITLE}
    )
    step = state.get("step")
    if step not in {
        WIZARD_STEP_TITLE,
        WIZARD_STEP_DATETIME,
        WIZARD_STEP_ZOOM,
        WIZARD_STEP_PAY,
        WIZARD_STEP_READY,
    }:
        state["step"] = WIZARD_STEP_TITLE
    return state


def _wizard_current_step(context: ContextTypes.DEFAULT_TYPE) -> str:
    state = _wizard_state(context)
    return str(state.get("step") or WIZARD_STEP_TITLE)


def _set_wizard_step(context: ContextTypes.DEFAULT_TYPE, step: str) -> None:
    state = _wizard_state(context)
    state["step"] = step


def _wizard_prompt(step: str) -> Optional[str]:
    # UX update: confirmation screen + cleaned messages
    prompts = {
        WIZARD_STEP_TITLE: "Введи название встречи.",
        WIZARD_STEP_DATETIME: "Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.",
        WIZARD_STEP_ZOOM: "Вставь ссылку на Zoom.",
        WIZARD_STEP_PAY: "Вставь ссылку на оплату.",
    }
    return prompts.get(step)


def _wizard_ready(draft: Dict[str, object]) -> bool:
    return bool(draft.get("title") and draft.get("datetime"))


def _draft_text(
    draft: Dict[str, object],
    step: str,
    status_message: Optional[str] = None,
) -> str:
    lines = ["🛠 Создание мероприятия"]
    lines.append(f"📛 Название: {html.escape((draft.get('title') or '').strip() or '—')}")
    lines.append(f"📅 Дата/время: {html.escape(_format_draft_datetime(draft))}")
    zoom = (draft.get("zoom_url") or "").strip() or "—"
    lines.append(f"🔗 Zoom: {html.escape(zoom)}")
    pay = (draft.get("pay_url") or "").strip() or "—"
    lines.append(f"💳 Оплата: {html.escape(pay)}")
    prompt = _wizard_prompt(step)
    if prompt:
        lines.append("")
        lines.append(prompt)
    if status_message:
        lines.append("")
        lines.append(status_message)
    return "\n".join(lines)


def _new_event_keyboard(ready: bool, step: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if step == WIZARD_STEP_ZOOM:
        rows.append([
            InlineKeyboardButton("⏭ Пропустить", callback_data="admin:new:skip_zoom")
        ])
    if step == WIZARD_STEP_PAY:
        rows.append([
            InlineKeyboardButton("⏭ Пропустить", callback_data="admin:new:skip_pay")
        ])
    if ready:
        rows.append([
            InlineKeyboardButton("✅ Завершить и создать", callback_data="admin:new:create")
        ])
    return InlineKeyboardMarkup(_add_home_button(rows))


async def _send_confirmation_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # UX update: confirmation screen + cleaned messages
    chat = update.effective_chat
    if chat is None:
        return
    draft = _draft(context)
    datetime_label = html.escape(_format_draft_datetime(draft))
    zoom_value = (draft.get("zoom_url") or "").strip()
    pay_value = (draft.get("pay_url") or "").strip()
    zoom_label = html.escape(zoom_value or "«не указана»")
    pay_label = html.escape(pay_value or "«не указана»")
    lines = [
        "🎯 Проверь данные:",
        "",
        f"📅 Дата и время: {datetime_label}",
        f"🔗 Zoom: {zoom_label}",
        f"💳 Оплата: {pay_label}",
        "",
        "Если всё верно — нажми кнопку ниже:",
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Завершить и создать", callback_data="admin:new:create")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="nav:main")],
        ]
    )
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text="\n".join(lines),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.debug("Failed to send confirmation screen", exc_info=True)


async def _show_new_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    draft = _draft(context)
    step = _wizard_current_step(context)
    ready = _wizard_ready(draft)
    text = _draft_text(draft, step, status_message)
    _replace_top(context, "new")
    await _send_wizard_panel(update, context, text, _new_event_keyboard(ready, step))


async def _prompt_wizard_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    prompt = _wizard_prompt(_wizard_current_step(context))
    if not prompt:
        return
    chat = update.effective_chat
    if not chat:
        return
    try:
        await context.bot.send_message(chat_id=chat.id, text=prompt)
    except Exception:
        logger.debug("Failed to deliver wizard prompt", exc_info=True)


async def _send_active_event_warning(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    if chat is not None:
        try:
            await context.bot.send_message(chat_id=chat.id, text=ACTIVE_EVENT_WARNING)
        except Exception:
            logger.debug("Failed to send active-event warning", exc_info=True)
    await _show_main_menu(update, context, status_message=ACTIVE_EVENT_WARNING)


async def _ensure_no_active_event(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if not has_active_event():
        return True
    await _close_wizard_panel(update, context)
    _clear_draft(context)
    _clear_await(context)
    await _send_active_event_warning(update, context)
    return False


def _format_event_detail(event: Event, status_message: Optional[str] = None) -> str:
    card = _format_event_card(event)
    lines = [card, "", "⚙️ Настройки мероприятия"]
    if status_message:
        lines.append("")
        lines.append(status_message)
    return "\n".join(lines)


def _event_menu_keyboard(event: Event) -> InlineKeyboardMarkup:
    base = f"admin:ev:{event.event_id}"
    status = classify_status(event)
    rows: List[List[InlineKeyboardButton]]
    if status == "active":
        rows = [
            [InlineKeyboardButton("✏️ Изменить название", callback_data=f"{base}:edit_title")],
            [InlineKeyboardButton("📝 Изменить описание", callback_data=f"{base}:edit_desc")],
            [InlineKeyboardButton("📅 Изменить дату и время", callback_data=f"{base}:edit_dt")],
            [InlineKeyboardButton("🔗 Обновить Zoom", callback_data=f"{base}:edit_zoom")],
            [InlineKeyboardButton("💳 Обновить оплату", callback_data=f"{base}:edit_pay")],
            [InlineKeyboardButton("📄 Просмотреть участников", callback_data=f"{base}:open_sheet")],
            [InlineKeyboardButton("🗑 Отменить мероприятие", callback_data=f"{base}:cancel")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"{base}:back")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📄 Просмотреть участников", callback_data=f"{base}:open_sheet")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"{base}:back")],
        ]
    return InlineKeyboardMarkup(_add_home_button(rows))


async def _show_event_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event_id: str,
    *,
    status_message: Optional[str] = None,
) -> None:
    event = get_event(event_id)
    if not event:
        await _show_event_list(update, context, page=1, status_message="Событие не найдено.")
        return
    _replace_top(context, "event", event_id=event_id)
    text = _format_event_detail(event, status_message)
    await _send_panel(update, context, text, _event_menu_keyboard(event))


async def _show_cancel_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event_id: str,
) -> None:
    event = get_event(event_id)
    if not event:
        await _show_event_list(update, context, page=1, status_message="Событие не найдено.")
        return
    text = _format_event_detail(event, "Вы уверены, что хотите отменить мероприятие?")
    keyboard = InlineKeyboardMarkup(
        _add_home_button(
            [
                [InlineKeyboardButton("✅ Да", callback_data=f"admin:ev:{event_id}:cancel_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data=f"admin:ev:{event_id}:cancel_no")],
            ]
        )
    )
    _replace_top(context, "event_cancel", event_id=event_id)
    await _send_panel(update, context, text, keyboard)


def _parse_datetime(text: str, timezone: str) -> datetime:
    variants = ["%d.%m.%Y %H:%M", "%d.%m %H:%M", "%Y-%m-%d %H:%M"]
    for fmt in variants:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%d.%m %H:%M":
                now = datetime.now(ZoneInfo(timezone))
                dt = dt.replace(year=now.year)
            return dt.replace(tzinfo=ZoneInfo(timezone))
        except ValueError:
            continue
    raise ValueError("invalid datetime")


async def _handle_new_event_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    if not await _ensure_no_active_event(update, context):
        return
    chat = update.effective_chat
    if data == "admin:new:skip_zoom":
        draft = _draft(context)
        draft["zoom_url"] = ""
        _set_wizard_step(context, WIZARD_STEP_PAY)
        context.user_data["await"] = {"type": "wizard", "step": WIZARD_STEP_PAY}
        if chat is not None:
            try:
                await context.bot.send_message(chat_id=chat.id, text="⏭ Zoom пропущен.")
            except Exception:
                logger.debug("Failed to send zoom skip confirmation", exc_info=True)
        await _show_new_event(update, context)
        await _prompt_wizard_step(update, context)
        return
    if data == "admin:new:skip_pay":
        draft = _draft(context)
        draft["pay_url"] = ""
        _set_wizard_step(context, WIZARD_STEP_READY)
        _clear_await(context)
        if chat is not None:
            try:
                await context.bot.send_message(chat_id=chat.id, text="⏭ Оплата пропущена.")
            except Exception:
                logger.debug("Failed to send payment skip confirmation", exc_info=True)
        await _show_new_event(update, context)
        await _send_confirmation_screen(update, context)
        return
    if data == "admin:new:create":
        draft = _draft(context)
        title = (draft.get("title") or "").strip()
        dt: Optional[datetime] = draft.get("datetime")
        if not title or not dt:
            await _show_new_event(
                update, context, status_message="Недостаточно данных для создания."
            )
            return
        timezone = str(draft.get("timezone") or TIMEZONE)
        zoom_url = (draft.get("zoom_url") or "").strip()
        pay_url = (draft.get("pay_url") or "").strip()
        event = create_event(
            title=title,
            description="",
            event_dt=dt,
            timezone=timezone,
            zoom_url=zoom_url,
            pay_url=pay_url,
        )
        ensure_scheduler_started()
        schedule_all_reminders(context.application)
        try:
            events_bootstrap(context.application.bot_data if context.application else None)
        except Exception:
            logger.exception("Failed to refresh events index after creation")
        formatted_dt = html.escape(_format_event_datetime(event))
        tz_label = html.escape(event.timezone or TIMEZONE)
        zoom_text = html.escape(event.zoom_url or "—")
        pay_text = html.escape(event.pay_url or "—")
        sheet_text = html.escape(event.sheet_link or "—")
        summary_lines = [
            "🎉 Встреча успешно создана!",
            "",
            f"📛 Название: {html.escape(event.title or '—')}",
            f"📅 Дата/время: {formatted_dt} ({tz_label})",
            f"🔗 Zoom: {zoom_text}",
            f"💳 Оплата: {pay_text}",
            f"📄 Участники: {sheet_text}",
            "",
            "Что дальше?",
        ]
        keyboard = InlineKeyboardMarkup(
            _add_home_button(
                [
                    [InlineKeyboardButton("🛠 Управление текущим", callback_data="admin:menu:manage")],
                    [InlineKeyboardButton("📣 Напомнить всем", callback_data="admin:menu:remind")],
                    [
                        InlineKeyboardButton(
                            "📄 Просмотр участников", callback_data="admin:menu:participants"
                        )
                    ],
                ]
            )
        )
        await _send_wizard_panel(update, context, "\n".join(summary_lines), keyboard)
        _clear_await(context)
        _clear_draft(context)
        _reset_stack(context)
        _push_entry(context, "main")
        await _show_main_menu(update, context)
        return


async def _handle_event_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    event_id: str,
    action: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    if action == "back":
        _pop_entry(context)
        await _show_event_list(update, context, page=1)
        return
    event = get_event(event_id)
    if not event:
        await _show_event_list(update, context, page=1, status_message="Событие не найдено.")
        return
    status = classify_status(event)
    editable = status == "active"
    view_only_message = "⚠️ Это мероприятие уже прошло.\nЕго можно только просмотреть 💗"
    if action == "edit_title":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_title", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Введите новое название.")
        return
    if action == "edit_desc":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_desc", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Введите новое описание.")
        return
    if action == "edit_dt":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _clear_await(context)
        context.user_data["await"] = {
            "type": "ev_edit_dt",
            "event_id": event_id,
            "timezone": event.timezone or TIMEZONE,
        }
        await _show_event_menu(
            update,
            context,
            event_id,
            status_message="Укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ",
        )
        return
    if action == "edit_zoom":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_zoom", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Отправьте новую ссылку на Zoom или пустое сообщение.")
        return
    if action == "edit_pay":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_pay", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Отправьте ссылку на оплату или пустое сообщение.")
        return
    if action == "open_sheet":
        try:
            link = open_sheet_url(event_id)
        except KeyError:
            await _show_event_menu(update, context, event_id, status_message="Ссылка недоступна.")
            return
        await _show_event_menu(update, context, event_id, status_message=f"Ссылка на участников: {link}")
        return
    if action == "cancel":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        _push_entry(context, "event_cancel", event_id=event_id)
        await _show_cancel_confirmation(update, context, event_id)
        return
    if action == "cancel_yes":
        if not editable:
            await _show_event_menu(update, context, event_id, status_message=view_only_message)
            return
        update_event(event_id, {"status": "cancelled"})
        if get_current_event_id() == event_id:
            set_current_event(None)
        await _show_event_list(update, context, page=1, status_message="Мероприятие отменено.")
        return
    if action == "cancel_no":
        _pop_entry(context)
        await _show_event_menu(update, context, event_id)
        return


async def _handle_wizard_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    step: str,
    text: str,
) -> None:
    if not await _ensure_no_active_event(update, context):
        return
    draft = _draft(context)
    if step == WIZARD_STEP_TITLE:
        if not text:
            await update.message.reply_text("Название не может быть пустым. Попробуй снова.")
            return
        draft["title"] = text
        await update.message.reply_text(f"✅ Название задано: {text}")
        _set_wizard_step(context, WIZARD_STEP_DATETIME)
        context.user_data["await"] = {"type": "wizard", "step": WIZARD_STEP_DATETIME}
        await _show_new_event(update, context)
        await _prompt_wizard_step(update, context)
        return
    if step == WIZARD_STEP_DATETIME:
        tz = draft.get("timezone") or TIMEZONE
        try:
            dt = _parse_datetime(text, tz)
        except ValueError:
            await update.message.reply_text(
                "Не удалось распознать дату. Попробуй снова в формате ДД.ММ.ГГГГ ЧЧ:ММ."
            )
            return
        now_local = datetime.now(ZoneInfo(tz))
        if dt <= now_local:
            await update.message.reply_text("⚠️ Эта дата уже прошла. Укажи будущую.")
            return
        draft["datetime"] = dt
        formatted = dt.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")
        await update.message.reply_text(f"✅ Дата и время установлены: {formatted}")
        _set_wizard_step(context, WIZARD_STEP_ZOOM)
        context.user_data["await"] = {"type": "wizard", "step": WIZARD_STEP_ZOOM}
        await _show_new_event(update, context)
        await _prompt_wizard_step(update, context)
        return
    if step == WIZARD_STEP_ZOOM:
        if text:
            draft["zoom_url"] = text
            await update.message.reply_text(
                f"✅ Zoom установлен: {text}", disable_web_page_preview=True
            )
        else:
            draft["zoom_url"] = ""
            await update.message.reply_text("⏭ Zoom пропущен.")
        _set_wizard_step(context, WIZARD_STEP_PAY)
        context.user_data["await"] = {"type": "wizard", "step": WIZARD_STEP_PAY}
        await _show_new_event(update, context)
        await _prompt_wizard_step(update, context)
        return
    if step == WIZARD_STEP_PAY:
        if text:
            draft["pay_url"] = text
            await update.message.reply_text(
                f"✅ Оплата установлена: {text}", disable_web_page_preview=True
            )
        else:
            draft["pay_url"] = ""
            await update.message.reply_text("⏭ Оплата пропущена.")
        _set_wizard_step(context, WIZARD_STEP_READY)
        _clear_await(context)
        await _show_new_event(update, context)
        await _send_confirmation_screen(update, context)
        return


async def _handle_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    await _close_wizard_panel(update, context)
    try:
        if data == "admin:menu:new":
            if not await _ensure_no_active_event(update, context):
                return
            _clear_draft(context)
            _clear_await(context)
            _push_entry(context, "new")
            _set_wizard_step(context, WIZARD_STEP_TITLE)
            context.user_data["await"] = {"type": "wizard", "step": WIZARD_STEP_TITLE}
            await _show_new_event(update, context)
            await _prompt_wizard_step(update, context)
            return
        if data == "admin:menu:list":
            _clear_await(context)
            _push_entry(context, "list", page=1)
            await _show_event_list(update, context, page=1)
            return
        if data == "admin:menu:manage":
            active = get_active_event()
            if not active:
                await _show_main_menu(
                    update, context, status_message="Нет активного мероприятия."
                )
                return
            _push_entry(context, "event", event_id=active.event_id)
            await _show_event_menu(update, context, active.event_id)
            return
        if data == "admin:menu:participants":
            event = get_active_event() or get_current_event()
            current = event.event_id if event else None
            if not current:
                await _show_main_menu(
                    update, context, status_message="Нет активного мероприятия."
                )
                return
            try:
                link = open_sheet_url(current)
            except Exception:
                logger.exception("Failed to open sheet link for %s", current)
                await _show_main_menu(
                    update,
                    context,
                    status_message="Не удалось получить ссылку на участников.",
                )
                return
            await _show_main_menu(
                update, context, status_message=f"Ссылка на участников: {link}"
            )
            return
        if data == "admin:menu:remind":
            _clear_await(context)
            _push_entry(context, "broadcast")
            context.user_data["await"] = {"type": "broadcast"}
            keyboard = InlineKeyboardMarkup(
                _add_home_button([[InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")]])
            )
            text = "Введите текст напоминания одним сообщением."
            await _send_panel(update, context, text, keyboard)
            return
    except Exception:
        logger.exception("Failed to handle admin menu callback")
        await _show_main_menu(
            update, context, status_message="Не удалось обработать запрос. Попробуйте позже."
        )


async def _handle_list_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    try:
        if data == "admin:list:back":
            _pop_entry(context)
            await _show_main_menu(update, context)
            return
        if data.startswith("admin:list:page:"):
            parts = data.split(":")
            page_part = parts[-1]
            entry = _current_entry(context) or {"data": {}}
            current_page = entry.get("data", {}).get("page", 1)
            if page_part == "noop":
                await _show_event_list(update, context, page=current_page)
                return
            try:
                requested_page = int(page_part)
            except ValueError:
                await _show_event_list(
                    update,
                    context,
                    page=current_page,
                    status_message="Некорректный номер страницы.",
                )
                return
            bot_data = context.application.bot_data if context.application else None
            events_refresh_if_stale(bot_data=bot_data)
            _, total_pages, total, actual_page = get_events_page(
                requested_page, PAGE_SIZE, bot_data=bot_data
            )
            if total == 0:
                await _show_event_list(update, context, page=1)
                return
            if requested_page < 1 or requested_page > total_pages:
                await _show_event_list(
                    update,
                    context,
                    page=actual_page,
                    status_message="Страница вне диапазона.",
                )
                return
            await _show_event_list(update, context, page=requested_page)
            return
        if data.startswith("admin:list:pick:"):
            event_id = data.split(":", 2)[2]
            _push_entry(context, "event", event_id=event_id)
            await _show_event_menu(update, context, event_id)
            return
    except Exception:
        logger.exception("Failed to handle admin list callback")
        await _show_event_list(
            update,
            context,
            page=1,
            status_message="Не удалось обработать запрос. Попробуйте позже.",
        )


async def _handle_nav_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    entry = _current_entry(context)
    if not entry:
        _reset_stack(context)
        _push_entry(context, "main")
        await _show_main_menu(update, context)
        return
    screen = entry.get("screen")
    _clear_await(context)
    if screen == "new":
        _pop_entry(context)
        _clear_draft(context)
        await _close_wizard_panel(update, context)
        await _show_main_menu(update, context)
        return
    if screen == "broadcast":
        _pop_entry(context)
        await _show_main_menu(update, context)
        return
    if screen == "event_cancel":
        data = entry.get("data", {})
        event_id = data.get("event_id") if isinstance(data, dict) else None
        _pop_entry(context)
        if event_id:
            await _show_event_menu(update, context, event_id)
        else:
            await _show_event_list(update, context, page=1)
        return
    _pop_entry(context)
    previous = _current_entry(context)
    if not previous:
        _reset_stack(context)
        _push_entry(context, "main")
        await _show_main_menu(update, context)
        return
    screen = previous.get("screen")
    data = previous.get("data", {})
    if screen == "main":
        await _show_main_menu(update, context)
    elif screen == "list":
        await _show_event_list(update, context, page=data.get("page", 1))
    elif screen == "event":
        await _show_event_menu(update, context, data.get("event_id"))
    elif screen == "new":
        await _show_new_event(update, context)


async def _handle_nav_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    _clear_await(context)
    _clear_draft(context)
    await _close_wizard_panel(update, context)
    _reset_stack(context)
    _push_entry(context, "main")
    await _show_main_menu(update, context)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await _ensure_admin(update):
        return
    data = query.data or ""
    if data.startswith("admin:menu:"):
        await _handle_menu_callback(update, context, data)
        return
    if data.startswith("admin:list:"):
        await _handle_list_callback(update, context, data)
        return
    if data.startswith("admin:new:"):
        await _handle_new_event_callback(update, context, data)
        return
    match = re.match(r"^admin:ev:([^:]+):(.*)$", data)
    if match:
        event_id = match.group(1)
        action = match.group(2)
        await _handle_event_callback(update, context, event_id, action)
        return
    if data == "nav:back":
        await _handle_nav_back(update, context)
        return
    if data == "nav:main":
        await _handle_nav_main(update, context)
        return


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return
    await_state = context.user_data.get("await")
    if not await_state:
        return
    text = (update.message.text or "").strip()
    state_type = await_state.get("type")
    if state_type == "wizard":
        step = str(await_state.get("step") or WIZARD_STEP_TITLE)
        await _handle_wizard_message(update, context, step, text)
        return
    if state_type == "broadcast":
        if not text:
            await update.message.reply_text("Сообщение не может быть пустым. Попробуйте снова.")
            return
        recipients = database.list_chat_ids()
        for chat_id in recipients:
            await context.bot.send_message(chat_id=chat_id, text=text)
        _clear_await(context)
        _pop_entry(context)
        await _show_main_menu(update, context, status_message="Рассылка отправлена.")
        return
    event_id = await_state.get("event_id")
    if not event_id:
        return
    if state_type == "ev_edit_title":
        if not text:
            await update.message.reply_text("Название не может быть пустым. Попробуйте снова.")
            return
        update_event(event_id, {"title": text})
        _clear_await(context)
        await _show_event_menu(update, context, event_id, status_message="Название обновлено.")
        return
    if state_type == "ev_edit_desc":
        update_event(event_id, {"description": text})
        _clear_await(context)
        await _show_event_menu(update, context, event_id, status_message="Описание обновлено.")
        return
    if state_type == "ev_edit_dt":
        tz = await_state.get("timezone", TIMEZONE)
        try:
            dt = _parse_datetime(text, tz)
        except ValueError:
            await update.message.reply_text("Не удалось распознать дату. Попробуйте снова.")
            return
        update_event(event_id, {"datetime_local": dt.isoformat(), "timezone": tz})
        if get_current_event_id() == event_id:
            ensure_scheduler_started()
            schedule_all_reminders(context.application)
        _clear_await(context)
        await _show_event_menu(update, context, event_id, status_message="Дата и время обновлены.")
        return
    if state_type == "ev_edit_zoom":
        update_event(event_id, {"zoom_url": text})
        _clear_await(context)
        await _show_event_menu(update, context, event_id, status_message="Ссылка Zoom обновлена.")
        return
    if state_type == "ev_edit_pay":
        update_event(event_id, {"pay_url": text})
        _clear_await(context)
        await _show_event_menu(update, context, event_id, status_message="Ссылка на оплату обновлена.")
        return
