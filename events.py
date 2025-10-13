"""Event storage and helpers for the admin panel."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Tuple

from zoneinfo import ZoneInfo

import database
from config import DATA_DIR, TIMEZONE, update_settings


TZ = ZoneInfo(TIMEZONE)
EVENTS_FILE = DATA_DIR / "events.json"
EVENTS_INDEX_FILE = DATA_DIR / "events_index.json"


logger = logging.getLogger(__name__)


_events_index_cache: Dict[str, Any] = {}
_bot_data_ref: Optional[MutableMapping[str, Any]] = None


@dataclass
class Event:
    event_id: str
    title: str
    description: str
    datetime_local: str
    timezone: str
    zoom_url: str
    pay_url: str
    sheet_name: str
    sheet_link: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @property
    def parsed_datetime(self) -> Optional[datetime]:
        raw = self.datetime_local
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            try:
                tz = ZoneInfo(self.timezone)
            except Exception:
                tz = TZ
            return dt.replace(tzinfo=tz)
        return dt


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _default_payload() -> Dict[str, object]:
    return {"current_event_id": None, "events": []}


def _load_payload() -> Dict[str, object]:
    _ensure_data_dir()
    if not EVENTS_FILE.exists():
        data = _default_payload()
        _save_payload(data)
        return data
    return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))


def _save_payload(data: Dict[str, object]) -> None:
    _ensure_data_dir()
    EVENTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _hydrate_event(item: Dict[str, object]) -> Event:
    return Event(
        event_id=str(item.get("event_id")),
        title=str(item.get("title", "")),
        description=str(item.get("description", "")),
        datetime_local=str(item.get("datetime_local", "")),
        timezone=str(item.get("timezone", TIMEZONE)),
        zoom_url=str(item.get("zoom_url", "")),
        pay_url=str(item.get("pay_url", "")),
        sheet_name=str(item.get("sheet_name", "")),
        sheet_link=str(item.get("sheet_link", "")),
        status=str(item.get("status", "active")),
        created_at=str(item.get("created_at", datetime.now(TZ).isoformat())),
        updated_at=str(item.get("updated_at", datetime.now(TZ).isoformat())),
    )


def _normalize_events(items: Iterable[Dict[str, object]]) -> List[Event]:
    return [_hydrate_event(item) for item in items]


def _write_events(events: List[Event], current_event_id: Optional[str]) -> None:
    payload = {
        "current_event_id": current_event_id,
        "events": [event.to_dict() for event in events],
    }
    _save_payload(payload)


def _resolve_bot_data(
    bot_data: Optional[MutableMapping[str, Any]] = None,
) -> Optional[MutableMapping[str, Any]]:
    global _bot_data_ref
    if bot_data is not None:
        _bot_data_ref = bot_data
        return bot_data
    return _bot_data_ref


def _load_index_file() -> Dict[str, Any]:
    if not EVENTS_INDEX_FILE.exists():
        return {}
    try:
        raw = EVENTS_INDEX_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read events index file: %s", exc)
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse events index file: %s", exc)
        return {}


def _save_index_file(state: Dict[str, Any]) -> None:
    _ensure_data_dir()
    try:
        EVENTS_INDEX_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Failed to write events index file: %s", exc)


def _set_index_state(
    state: Dict[str, Any], bot_data: Optional[MutableMapping[str, Any]] = None
) -> None:
    global _events_index_cache
    _events_index_cache = state
    resolved = _resolve_bot_data(bot_data)
    if resolved is not None:
        resolved["events_index"] = state
    _save_index_file(state)


def _get_index_state(
    bot_data: Optional[MutableMapping[str, Any]] = None,
) -> Dict[str, Any]:
    resolved = _resolve_bot_data(bot_data)
    if _events_index_cache:
        return _events_index_cache
    if resolved and isinstance(resolved.get("events_index"), dict):
        _events_index_cache.update(resolved["events_index"])
        return _events_index_cache
    file_payload = _load_index_file()
    if file_payload:
        _events_index_cache.update(file_payload)
        if resolved is not None:
            resolved["events_index"] = file_payload
    return _events_index_cache


SHEET_NAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}__[\w-]+(?:-\d+)?$")


def _collect_sheet_index() -> Dict[str, Dict[str, Any]]:
    try:
        worksheets = database.list_event_sheets()
    except Exception as exc:  # pragma: no cover - network errors
        logger.warning("Failed to collect sheet index: %s", exc)
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for sheet in worksheets:
        name = str(sheet.get("sheet_name") or sheet.get("title") or "")
        if not name:
            continue
        if name in {"Sheet1", "Лист1"}:
            continue
        if not SHEET_NAME_PATTERN.match(name):
            continue
        result[name] = {
            "sheet_name": name,
            "sheet_id": sheet.get("sheet_id"),
            "sheet_index": sheet.get("sheet_index"),
        }
    return result


def _sorted_events(events: List[Event]) -> List[Event]:
    ordered = list(events)

    def sort_key(event: Event) -> Tuple[int, float, str]:
        order = {"active": 0, "past": 1, "cancelled": 2}.get(
            classify_status(event), 3
        )
        dt = event.parsed_datetime
        ts = dt.timestamp() if dt else 0.0
        title = (event.title or "").lower()
        return (order, -ts, title)

    ordered.sort(key=sort_key)
    return ordered


def _placeholder_event_dict(event_id: str) -> Dict[str, Any]:
    now_iso = datetime.now(TZ).isoformat()
    return {
        "event_id": event_id,
        "title": event_id,
        "description": "",
        "datetime_local": "",
        "timezone": TIMEZONE,
        "zoom_url": "",
        "pay_url": "",
        "sheet_name": event_id,
        "sheet_link": "",
        "status": "active",
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def _build_index_items(events: List[Event]) -> List[Dict[str, Any]]:
    sheet_map = _collect_sheet_index()
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for event in _sorted_events(events):
        sheet_key = event.sheet_name or event.event_id
        sheet_info = sheet_map.get(sheet_key) or sheet_map.get(event.event_id)
        items.append(
            {
                "event_id": event.event_id,
                "sheet_name": sheet_info.get("sheet_name") if sheet_info else sheet_key,
                "sheet_id": sheet_info.get("sheet_id") if sheet_info else None,
                "sheet_index": sheet_info.get("sheet_index") if sheet_info else None,
                "event": event.to_dict(),
            }
        )
        seen.add(sheet_key)
        seen.add(event.event_id)
    for sheet_name, sheet_info in sheet_map.items():
        if sheet_name in seen:
            continue
        items.append(
            {
                "event_id": sheet_name,
                "sheet_name": sheet_info.get("sheet_name") or sheet_name,
                "sheet_id": sheet_info.get("sheet_id"),
                "sheet_index": sheet_info.get("sheet_index"),
                "event": _placeholder_event_dict(sheet_name),
            }
        )
    return items


def _pick_latest_event_id(events: List[Event]) -> Optional[str]:
    latest_id: Optional[str] = None
    latest_dt: Optional[datetime] = None
    for event in events:
        dt = event.parsed_datetime
        if not dt:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_id = event.event_id
    if latest_id:
        return latest_id
    if events:
        return events[0].event_id
    return None


def _mark_index_stale() -> None:
    if isinstance(_events_index_cache, dict):
        _events_index_cache["fetched_at"] = None
    resolved = _resolve_bot_data()
    if resolved and isinstance(resolved.get("events_index"), dict):
        resolved["events_index"]["fetched_at"] = None


def load_events() -> Tuple[List[Event], Optional[str]]:
    payload = _load_payload()
    events = _normalize_events(payload.get("events", []))
    current_id = payload.get("current_event_id")
    current_id = str(current_id) if current_id else None
    return events, current_id


def _store_events(events: List[Event], current_event_id: Optional[str]) -> None:
    _write_events(events, current_event_id)


def get_event(event_id: str) -> Optional[Event]:
    events, current_id = load_events()
    for event in events:
        if event.event_id == event_id:
            _auto_update_status(events, current_id)
            return event
    return None


def get_current_event_id() -> Optional[str]:
    _, current_id = load_events()
    return current_id


def get_current_event() -> Optional[Event]:
    current_id = get_current_event_id()
    if not current_id:
        return None
    return get_event(current_id)


def events_bootstrap(
    bot_data: Optional[MutableMapping[str, Any]] = None,
) -> Optional[str]:
    resolved_bot_data = _resolve_bot_data(bot_data)
    events, stored_current = load_events()
    items = _build_index_items(events)
    current_event_id: Optional[str] = None
    if stored_current and any(event.event_id == stored_current for event in events):
        current_event_id = stored_current
    else:
        current_event_id = _pick_latest_event_id(events)
        if current_event_id != stored_current:
            set_current_event(current_event_id)
    state = {
        "fetched_at": datetime.now(TZ).isoformat(),
        "items": items,
        "current_event_id": current_event_id,
    }
    _set_index_state(state, resolved_bot_data)
    return current_event_id


def set_current_event(event_id: Optional[str]) -> None:
    events, _ = load_events()
    _auto_update_status(events, event_id)
    _store_events(events, event_id)
    if event_id:
        event = next((ev for ev in events if ev.event_id == event_id), None)
    else:
        event = None
    if event:
        worksheet = database.get_or_create_sheet(event.sheet_name)
        update_settings(
            topic=event.title,
            description=event.description,
            zoom_link=event.zoom_url,
            payment_link=event.pay_url,
            current_event_id=event.event_id,
            current_event_sheet_name=event.sheet_name,
            current_event_sheet_gid=worksheet.id,
            current_event_datetime=event.datetime_local,
            timezone=event.timezone,
        )
    else:
        update_settings(
            current_event_id=None,
            current_event_sheet_name=None,
            current_event_sheet_gid=None,
            current_event_datetime=None,
        )
    _mark_index_stale()


def _event_local_datetime(event: Event) -> Optional[datetime]:
    dt = event.parsed_datetime
    if dt is None:
        return None
    try:
        tz = dt.tzinfo or ZoneInfo(event.timezone or TIMEZONE)
    except Exception:
        tz = TZ
    return dt.astimezone(tz)


def _auto_update_status(events: List[Event], current_event_id: Optional[str]) -> None:
    changed = False
    for event in events:
        if event.status == "cancelled":
            continue
        local_dt = _event_local_datetime(event)
        if local_dt is None:
            continue
        now_local = datetime.now(local_dt.tzinfo)
        status = "past" if local_dt <= now_local else "active"
        if event.status != status:
            event.status = status
            event.updated_at = datetime.now(TZ).isoformat()
            changed = True
    if changed:
        _store_events(events, current_event_id)


def classify_status(event: Event) -> str:
    if event.status == "cancelled":
        return "cancelled"
    local_dt = _event_local_datetime(event)
    if local_dt is None:
        return event.status or "active"
    now_local = datetime.now(local_dt.tzinfo)
    return "past" if local_dt <= now_local else "active"


def has_active_event() -> bool:
    events, current_id = load_events()
    _auto_update_status(events, current_id)
    for event in events:
        if classify_status(event) == "active":
            return True
    return False


def get_active_event() -> Optional[Event]:
    events, current_id = load_events()
    _auto_update_status(events, current_id)
    preferred: Optional[Event] = None
    if current_id:
        preferred = next((event for event in events if event.event_id == current_id), None)
        if preferred and classify_status(preferred) == "active":
            return preferred
    active_events = [event for event in events if classify_status(event) == "active"]
    if not active_events:
        return None
    def sort_key(event: Event) -> datetime:
        dt = event.parsed_datetime
        if dt is None:
            return datetime.max.replace(tzinfo=TZ)
        try:
            return dt.astimezone(TZ)
        except Exception:
            return datetime.max.replace(tzinfo=TZ)

    active_events.sort(key=sort_key)
    return active_events[0]


def events_refresh_if_stale(
    max_age_min: int = 5,
    *,
    bot_data: Optional[MutableMapping[str, Any]] = None,
) -> None:
    state = _get_index_state(bot_data)
    fetched_raw = state.get("fetched_at") if isinstance(state, dict) else None
    if not fetched_raw:
        events_bootstrap(bot_data)
        return
    try:
        fetched_dt = datetime.fromisoformat(str(fetched_raw))
    except ValueError:
        events_bootstrap(bot_data)
        return
    if datetime.now(TZ) - fetched_dt >= timedelta(minutes=max_age_min):
        events_bootstrap(bot_data)


def list_events(
    page: int,
    page_size: int,
    status_filter: Optional[Iterable[str]] = None,
) -> Tuple[List[Event], int, int]:
    events, current_id = load_events()
    _auto_update_status(events, current_id)
    if status_filter is not None:
        allowed = {status for status in status_filter}
        filtered = [event for event in events if classify_status(event) in allowed]
    else:
        filtered = events

    def sort_key(event: Event) -> Tuple[int, float]:
        order = {"active": 0, "past": 1, "cancelled": 2}.get(
            classify_status(event), 3
        )
        dt = event.parsed_datetime
        ts = dt.timestamp() if dt else 0.0
        return (order, -ts)

    filtered.sort(key=sort_key)
    total = len(filtered)
    if page_size <= 0:
        page_size = 5
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return filtered[start:end], total_pages, total


def _hydrate_entry(entry: Dict[str, Any], fallback: Optional[Event] = None) -> Event:
    if fallback is not None:
        return fallback
    payload = entry.get("event") if isinstance(entry, dict) else None
    if isinstance(payload, dict):
        return _hydrate_event(payload)
    return _hydrate_event(_placeholder_event_dict(str(entry.get("event_id"))))


def get_events_page(
    page: int,
    size: int,
    *,
    bot_data: Optional[MutableMapping[str, Any]] = None,
) -> Tuple[List[Event], int, int, int]:
    state = _get_index_state(bot_data)
    if not state:
        events_bootstrap(bot_data)
        state = _get_index_state(bot_data)
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, list):
        items = []
    total = len(items)
    if size <= 0:
        size = 5
    if total == 0:
        return [], 0, 0, 1
    total_pages = (total + size - 1) // size
    page = max(1, min(page, total_pages))
    start = (page - 1) * size
    end = start + size
    page_items = items[start:end]
    events, current_id = load_events()
    _auto_update_status(events, current_id)
    event_map = {event.event_id: event for event in events}
    hydrated: List[Event] = []
    for entry in page_items:
        event_id = entry.get("event_id") if isinstance(entry, dict) else None
        fallback = event_map.get(str(event_id)) if event_id else None
        hydrated.append(_hydrate_entry(entry, fallback))
    return hydrated, total_pages, total, page


def _find_event_index(events: List[Event], event_id: str) -> int:
    for idx, event in enumerate(events):
        if event.event_id == event_id:
            return idx
    raise KeyError(event_id)


def update_event(event_id: str, fields: Dict[str, object]) -> Event:
    events, current_id = load_events()
    idx = _find_event_index(events, event_id)
    event = events[idx]
    for key, value in fields.items():
        if not hasattr(event, key):
            continue
        setattr(event, key, value if value is not None else getattr(event, key))
    event.updated_at = datetime.now(TZ).isoformat()
    events[idx] = event
    _store_events(events, current_id)
    if current_id == event_id:
        set_current_event(event_id)
    _mark_index_stale()
    return event


def create_event_sheet(event_id: str) -> Tuple[str, str]:
    worksheet = database.get_or_create_sheet(event_id)
    link = database.get_sheet_link(event_id, worksheet.id)
    return event_id, link


def open_sheet_url(event_id: str) -> str:
    event = get_event(event_id)
    if not event:
        raise KeyError(event_id)
    if event.sheet_link:
        return event.sheet_link
    sheet_name, link = create_event_sheet(event.event_id)
    payload: Dict[str, object] = {"sheet_link": link}
    if event.sheet_name != sheet_name:
        payload["sheet_name"] = sheet_name
    update_event(event_id, payload)
    return link


def _slugify(text: str) -> str:
    normalized = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    normalized = normalized.strip().lower()
    normalized = re.sub(r"[\s./]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized)
    return normalized or "event"


def _generate_event_id(title: str, event_dt: datetime) -> str:
    slug = _slugify(title)
    date_part = event_dt.astimezone(TZ).strftime("%Y-%m-%d")
    base = f"{date_part}__{slug}"
    events, _ = load_events()
    existing_ids = {event.event_id for event in events}
    candidate = base
    suffix = 1
    while candidate in existing_ids:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def create_event(
    *,
    title: str,
    description: str,
    event_dt: datetime,
    timezone: str,
    zoom_url: str,
    pay_url: str,
) -> Event:
    events, current_id = load_events()
    now_iso = datetime.now(TZ).isoformat()
    _auto_update_status(events, current_id)
    for existing in events:
        if existing.status == "cancelled":
            continue
        if existing.status != "past":
            existing.status = "past"
            existing.updated_at = now_iso
    event_id = _generate_event_id(title, event_dt)
    sheet_name, sheet_link = create_event_sheet(event_id)
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = TZ
        timezone = TIMEZONE
    event = Event(
        event_id=event_id,
        title=title,
        description=description,
        datetime_local=event_dt.astimezone(tz).isoformat(),
        timezone=timezone,
        zoom_url=zoom_url,
        pay_url=pay_url,
        sheet_name=sheet_name,
        sheet_link=sheet_link,
        status="active",
        created_at=now_iso,
        updated_at=now_iso,
    )
    events.append(event)
    _store_events(events, current_id)
    set_current_event(event_id)
    _mark_index_stale()
    return event


