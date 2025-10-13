"""Admin inline interface with hierarchical navigation."""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

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
    get_current_event,
    get_current_event_id,
    get_event,
    list_events,
    open_sheet_url,
    set_current_event,
    update_event,
)
from scheduler import ensure_scheduler_started, schedule_all_reminders

TZ = ZoneInfo(TIMEZONE)
PAGE_SIZE = 5
TIMEZONE_PRESETS: List[Tuple[str, str]] = [
    ("Europe/Moscow", "Europe/Moscow (UTC+3)"),
    ("Europe/Kaliningrad", "Europe/Kaliningrad (UTC+2)"),
    ("Europe/Berlin", "Europe/Berlin (UTC+1/+2)"),
    ("Asia/Almaty", "Asia/Almaty (UTC+6)"),
    ("Asia/Vladivostok", "Asia/Vladivostok (UTC+10)"),
    ("UTC", "UTC"),
]

EMOJI_NUMBERS = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
    9: "9️⃣",
    10: "🔟",
}


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


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🆕 Новое мероприятие", callback_data="admin:menu:new")],
        [InlineKeyboardButton("📅 Мои мероприятия", callback_data="admin:menu:list")],
        [InlineKeyboardButton("📄 Просмотр участников", callback_data="admin:menu:participants")],
        [InlineKeyboardButton("📣 Напомнить всем", callback_data="admin:menu:remind")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    event = get_current_event()
    text = _format_event_card(event, status_message)
    await _send_panel(update, context, text, _main_menu_keyboard())


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
        buttons: List[InlineKeyboardButton] = []
        for idx, event in enumerate(events, start=1):
            label = EMOJI_NUMBERS.get(idx, str(idx))
            buttons.append(
                InlineKeyboardButton(label, callback_data=f"admin:list:pick:{event.event_id}")
            )
        rows.append(buttons)
    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ Назад",
                callback_data=f"admin:list:page:{prev_page}" if page > 1 else f"admin:list:page:{page}",
            ),
            InlineKeyboardButton(f"Стр. {page}/{total_pages}", callback_data="admin:list:page:noop"),
            InlineKeyboardButton(
                "Вперёд ▶️",
                callback_data=f"admin:list:page:{next_page}" if page < total_pages else f"admin:list:page:{page}",
            ),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:list:back")])
    return InlineKeyboardMarkup(rows)


async def _show_event_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 1,
    status_message: Optional[str] = None,
) -> None:
    events, total_pages, total = list_events(page, PAGE_SIZE)
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
    keyboard = _list_keyboard(events, page, total_pages if total else 1)
    _replace_top(context, "list", page=page)
    await _send_panel(update, context, text, keyboard)


def _draft(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, object]:
    draft = context.user_data.setdefault(
        "draft_event",
        {
            "title": "",
            "description": "",
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
    return _format_event_datetime(
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


def _draft_text(draft: Dict[str, object], status_message: Optional[str] = None) -> str:
    lines = ["🛠 Создание мероприятия"]
    lines.append(f"📛 Название: {html.escape((draft.get('title') or '').strip() or '—')}")
    desc = (draft.get("description") or "").strip() or "—"
    lines.append(f"📝 Описание: {html.escape(desc)}")
    lines.append(f"📅 Дата и время: {html.escape(_format_draft_datetime(draft))}")
    lines.append(f"🌍 Часовой пояс: {html.escape(str(draft.get('timezone') or TIMEZONE))}")
    zoom = (draft.get("zoom_url") or "").strip() or "—"
    lines.append(f"🔗 Zoom: {html.escape(zoom)}")
    pay = (draft.get("pay_url") or "").strip() or "—"
    lines.append(f"💳 Оплата: {html.escape(pay)}")
    if status_message:
        lines.append("")
        lines.append(status_message)
    return "\n".join(lines)


def _new_event_keyboard(ready: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📛 Название", callback_data="admin:new:title")],
        [InlineKeyboardButton("📝 Описание", callback_data="admin:new:desc")],
        [InlineKeyboardButton("📅 Дата и время", callback_data="admin:new:dt")],
        [InlineKeyboardButton("🔗 Zoom", callback_data="admin:new:zoom")],
        [InlineKeyboardButton("💳 Оплата", callback_data="admin:new:pay")],
        [InlineKeyboardButton("🌍 Часовой пояс", callback_data="admin:new:tz")],
    ]
    rows.append([InlineKeyboardButton("✅ Завершить и создать", callback_data="admin:new:confirm")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")])
    return InlineKeyboardMarkup(rows)


async def _show_new_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status_message: Optional[str] = None,
) -> None:
    draft = _draft(context)
    ready = bool(draft.get("title") and draft.get("datetime"))
    text = _draft_text(draft, status_message)
    _replace_top(context, "new")
    await _send_panel(update, context, text, _new_event_keyboard(ready))


async def _show_timezone_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    draft = _draft(context)
    current = str(draft.get("timezone") or TIMEZONE)
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if current == tz else "") + label,
                callback_data=f"admin:new:tzset:{tz}",
            )
        ]
        for tz, label in TIMEZONE_PRESETS
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")])
    text = "Выберите часовой пояс"
    _replace_top(context, "new_tz")
    await _send_panel(update, context, text, InlineKeyboardMarkup(rows))


async def _show_new_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    draft = _draft(context)
    text = _draft_text(draft, "Проверьте данные и подтвердите создание.")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Создать", callback_data="admin:new:create")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")],
        ]
    )
    _replace_top(context, "new_confirm")
    await _send_panel(update, context, text, keyboard)


def _format_event_detail(event: Event, status_message: Optional[str] = None) -> str:
    card = _format_event_card(event)
    lines = [card, "", "⚙️ Настройки мероприятия"]
    if status_message:
        lines.append("")
        lines.append(status_message)
    return "\n".join(lines)


def _event_menu_keyboard(event: Event) -> InlineKeyboardMarkup:
    base = f"admin:ev:{event.event_id}"
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
    return InlineKeyboardMarkup(rows)


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
        [
            [InlineKeyboardButton("✅ Да", callback_data=f"admin:ev:{event_id}:cancel_yes")],
            [InlineKeyboardButton("❌ Нет", callback_data=f"admin:ev:{event_id}:cancel_no")],
        ]
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
    if data == "admin:new:title":
        _clear_await(context)
        context.user_data["await"] = {"type": "new_title"}
        await _show_new_event(update, context, status_message="Введите название мероприятия текстом.")
        return
    if data == "admin:new:desc":
        _clear_await(context)
        context.user_data["await"] = {"type": "new_desc"}
        await _show_new_event(update, context, status_message="Отправьте описание мероприятия.")
        return
    if data == "admin:new:dt":
        draft = _draft(context)
        tz = draft.get("timezone") or TIMEZONE
        _clear_await(context)
        context.user_data["await"] = {"type": "new_dt", "timezone": tz}
        await _show_new_event(
            update,
            context,
            status_message="Укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ",
        )
        return
    if data == "admin:new:zoom":
        _clear_await(context)
        context.user_data["await"] = {"type": "new_zoom"}
        await _show_new_event(update, context, status_message="Отправьте ссылку Zoom или пустое сообщение.")
        return
    if data == "admin:new:pay":
        _clear_await(context)
        context.user_data["await"] = {"type": "new_pay"}
        await _show_new_event(update, context, status_message="Отправьте ссылку на оплату или пустое сообщение.")
        return
    if data == "admin:new:tz":
        _clear_await(context)
        _push_entry(context, "new_tz")
        await _show_timezone_picker(update, context)
        return
    if data.startswith("admin:new:tzset:"):
        tz = data.split(":", 3)[3]
        draft = _draft(context)
        draft["timezone"] = tz
        _pop_entry(context)
        await _show_new_event(update, context, status_message=f"Часовой пояс установлен: {tz}")
        return
    if data == "admin:new:confirm":
        draft = _draft(context)
        if not draft.get("title") or not draft.get("datetime"):
            await _show_new_event(update, context, status_message="Укажите название и дату.")
            return
        _clear_await(context)
        _push_entry(context, "new_confirm")
        await _show_new_confirm(update, context)
        return
    if data == "admin:new:create":
        draft = _draft(context)
        title = (draft.get("title") or "").strip()
        dt: Optional[datetime] = draft.get("datetime")
        if not title or not dt:
            await _show_new_event(update, context, status_message="Недостаточно данных для создания.")
            return
        description = (draft.get("description") or "").strip()
        timezone = str(draft.get("timezone") or TIMEZONE)
        zoom_url = (draft.get("zoom_url") or "").strip()
        pay_url = (draft.get("pay_url") or "").strip()
        event = create_event(
            title=title,
            description=description,
            event_dt=dt,
            timezone=timezone,
            zoom_url=zoom_url,
            pay_url=pay_url,
        )
        ensure_scheduler_started()
        schedule_all_reminders(context.application)
        _clear_draft(context)
        _clear_await(context)
        _reset_stack(context)
        _push_entry(context, "main")
        await _show_main_menu(
            update,
            context,
            status_message=f"Мероприятие создано: {html.escape(event.title)}",
        )
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
    if action == "edit_title":
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_title", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Введите новое название.")
        return
    if action == "edit_desc":
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_desc", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Введите новое описание.")
        return
    if action == "edit_dt":
        event = get_event(event_id)
        if not event:
            await _show_event_list(update, context, page=1, status_message="Событие не найдено.")
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
        _clear_await(context)
        context.user_data["await"] = {"type": "ev_edit_zoom", "event_id": event_id}
        await _show_event_menu(update, context, event_id, status_message="Отправьте новую ссылку на Zoom или пустое сообщение.")
        return
    if action == "edit_pay":
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
        _push_entry(context, "event_cancel", event_id=event_id)
        await _show_cancel_confirmation(update, context, event_id)
        return
    if action == "cancel_yes":
        update_event(event_id, {"status": "cancelled"})
        if get_current_event_id() == event_id:
            set_current_event(None)
        await _show_event_list(update, context, page=1, status_message="Мероприятие отменено.")
        return
    if action == "cancel_no":
        _pop_entry(context)
        await _show_event_menu(update, context, event_id)
        return


async def _handle_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    if data == "admin:menu:new":
        _clear_draft(context)
        _clear_await(context)
        _push_entry(context, "new")
        await _show_new_event(update, context, status_message="Заполните карточку мероприятия шаг за шагом.")
        return
    if data == "admin:menu:list":
        _clear_await(context)
        _push_entry(context, "list", page=1)
        await _show_event_list(update, context, page=1)
        return
    if data == "admin:menu:participants":
        current = get_current_event_id()
        if not current:
            await _show_main_menu(update, context, status_message="Нет активного мероприятия.")
            return
        link = open_sheet_url(current)
        await _show_main_menu(update, context, status_message=f"Ссылка на участников: {link}")
        return
    if data == "admin:menu:remind":
        _clear_await(context)
        _push_entry(context, "broadcast")
        context.user_data["await"] = {"type": "broadcast"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")]])
        text = "Введите текст напоминания одним сообщением."
        await _send_panel(update, context, text, keyboard)
        return


async def _handle_list_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    if data == "admin:list:back":
        _pop_entry(context)
        await _show_main_menu(update, context)
        return
    if data.startswith("admin:list:page:"):
        parts = data.split(":")
        page_part = parts[-1]
        entry = _current_entry(context) or {"data": {}}
        if page_part == "noop":
            page = entry.get("data", {}).get("page", 1)
        else:
            try:
                page = max(1, int(page_part))
            except ValueError:
                page = 1
        await _show_event_list(update, context, page=page)
        return
    if data.startswith("admin:list:pick:"):
        event_id = data.split(":", 2)[2]
        _push_entry(context, "event", event_id=event_id)
        await _show_event_menu(update, context, event_id)
        return


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
        await _show_main_menu(update, context)
        return
    if screen in {"new_tz", "new_confirm"}:
        _pop_entry(context)
        await _show_new_event(update, context)
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


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return
    await_state = context.user_data.get("await")
    if not await_state:
        return
    text = (update.message.text or "").strip()
    state_type = await_state.get("type")
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
    if state_type == "new_title":
        if not text:
            await update.message.reply_text("Название не может быть пустым. Попробуйте снова.")
            return
        _draft(context)["title"] = text
        _clear_await(context)
        await _show_new_event(update, context, status_message="Название сохранено.")
        return
    if state_type == "new_desc":
        _draft(context)["description"] = text
        _clear_await(context)
        await _show_new_event(update, context, status_message="Описание обновлено.")
        return
    if state_type == "new_dt":
        tz = await_state.get("timezone", TIMEZONE)
        try:
            dt = _parse_datetime(text, tz)
        except ValueError:
            await update.message.reply_text("Не удалось распознать дату. Попробуйте снова.")
            return
        _draft(context)["datetime"] = dt
        _clear_await(context)
        await _show_new_event(update, context, status_message="Дата и время сохранены.")
        return
    if state_type == "new_zoom":
        _draft(context)["zoom_url"] = text
        _clear_await(context)
        await _show_new_event(update, context, status_message="Ссылка Zoom сохранена.")
        return
    if state_type == "new_pay":
        _draft(context)["pay_url"] = text
        _clear_await(context)
        await _show_new_event(update, context, status_message="Ссылка на оплату сохранена.")
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
