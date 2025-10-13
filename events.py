"""Event storage and helpers for the admin panel."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from zoneinfo import ZoneInfo

import database
from config import DATA_DIR, TIMEZONE, update_settings


TZ = ZoneInfo(TIMEZONE)
EVENTS_FILE = DATA_DIR / "events.json"


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


def _auto_update_status(events: List[Event], current_event_id: Optional[str]) -> None:
    changed = False
    now = datetime.now(TZ)
    for event in events:
        if event.status == "cancelled":
            continue
        dt = event.parsed_datetime
        if not dt:
            continue
        status = "past" if dt < now else "active"
        if event.status != status:
            event.status = status
            event.updated_at = datetime.now(TZ).isoformat()
            changed = True
    if changed:
        _store_events(events, current_event_id)


def classify_status(event: Event) -> str:
    if event.status == "cancelled":
        return "cancelled"
    dt = event.parsed_datetime
    if dt is None:
        return event.status or "active"
    return "past" if dt < datetime.now(dt.tzinfo or TZ) else "active"


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
    return event


def create_event_sheet(event_id: str) -> Tuple[str, str]:
    worksheet = database.get_or_create_sheet(event_id)
    link = database.get_sheet_link(event_id, worksheet.id)
    return worksheet.title, link


def open_sheet_url(event_id: str) -> str:
    event = get_event(event_id)
    if not event:
        raise KeyError(event_id)
    if event.sheet_link:
        return event.sheet_link
    link = database.get_sheet_link(event.sheet_name)
    update_event(event_id, {"sheet_link": link})
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
    event_id = _generate_event_id(title, event_dt)
    sheet_name, sheet_link = create_event_sheet(event_id)
    now_iso = datetime.now(TZ).isoformat()
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
    return event


