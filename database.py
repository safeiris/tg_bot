"""Google Sheets backed storage for webinar participants."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo

from config import (
    DATA_DIR,
    GSPREAD_CREDENTIALS_PATH,
    GSPREAD_SHEET_ID,
    TIMEZONE,
    load_settings,
)

from gspread.utils import rowcol_to_a1


SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

HEADERS = [
    "Время регистрации",
    "chat_id",
    "Имя пользователя",
    "Имя",
    "Email",
    "Тип участия",
    "Статус оплаты",
    "Обратная связь",
]

ROLE_FREE = "Участник (бесплатно)"
ROLE_PAID = "Разбор (платно)"
PAYMENT_PAID = "Оплачено"
PAYMENT_UNPAID = "Не оплачено"

TZ = ZoneInfo(TIMEZONE)

_client: gspread.Client | None = None


@dataclass
class Participant:
    name: str
    username: str
    chat_id: int
    email: str
    role: str = ROLE_FREE
    paid: str = "no"
    feedback: str = ""


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        credentials = Credentials.from_service_account_file(
            GSPREAD_CREDENTIALS_PATH, scopes=SCOPES
        )
        _client = gspread.authorize(credentials)
    return _client


def _open_spreadsheet() -> gspread.Spreadsheet:
    client = _get_client()
    return client.open_by_key(GSPREAD_SHEET_ID)


def _ensure_headers(worksheet: gspread.Worksheet) -> List[str]:
    current_headers = [h.strip() for h in worksheet.row_values(1)]
    if current_headers != HEADERS:
        worksheet.update("1:1", [HEADERS])
        current_headers = HEADERS.copy()
    return current_headers


def _header_map(worksheet: gspread.Worksheet) -> Dict[str, int]:
    headers = _ensure_headers(worksheet)
    return {header: idx + 1 for idx, header in enumerate(headers)}


def _row_dict(
    worksheet: gspread.Worksheet, row_idx: int, header_map: Dict[str, int]
) -> Dict[str, str]:
    values = worksheet.row_values(row_idx)
    row_data: Dict[str, str] = {}
    for header, col_idx in header_map.items():
        list_idx = col_idx - 1
        row_data[header] = values[list_idx] if list_idx < len(values) else ""
    return row_data


def _format_role(value: str) -> str:
    normalized = (value or "").strip().lower()
    paid_aliases = {
        "paid",
        "платный",
        "платно",
        "платная",
        "платное",
        "платные",
        "участник",
        "участник с разбором",
        "участие с разбором",
        "с разбором",
        "участник с разбором (платно)",
        "разбор",
        ROLE_PAID.lower(),
    }
    free_aliases = {
        "free",
        "наблюдатель",
        "наблюдатель (бесплатно)",
        "бесплатно",
        ROLE_FREE.lower(),
    }
    if normalized in paid_aliases:
        return ROLE_PAID
    if normalized in free_aliases:
        return ROLE_FREE
    if "разбор" in normalized:
        return ROLE_PAID
    return ROLE_FREE


def format_role(value: str) -> str:
    """Return a normalized role label for display purposes."""

    return _format_role(value)


def _format_payment_status(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {
        "yes",
        "y",
        "true",
        "1",
        PAYMENT_PAID.lower(),
        "оплатил",
        "оплатила",
        "оплачен",
        "оплачена",
    }:
        return PAYMENT_PAID
    return PAYMENT_UNPAID


def create_event_sheet(sheet_name: str) -> gspread.Worksheet:
    spreadsheet = _open_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=len(HEADERS))
    _ensure_headers(worksheet)
    return worksheet


def get_sheet_by_name(sheet_name: str) -> Optional[gspread.Worksheet]:
    spreadsheet = _open_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return None
    _ensure_headers(worksheet)
    return worksheet


def list_event_sheets() -> List[Dict[str, object]]:
    spreadsheet = _open_spreadsheet()
    worksheets = spreadsheet.worksheets()
    sheets: List[Dict[str, object]] = []
    for worksheet in worksheets:
        sheets.append(
            {
                "sheet_name": worksheet.title,
                "sheet_id": worksheet.id,
                "sheet_index": getattr(worksheet, "index", None),
            }
        )
    return sheets


def list_sheet_tabs() -> set[str]:
    """Return the set of available sheet/tab names in the spreadsheet."""
    spreadsheet = _open_spreadsheet()
    try:
        worksheets = spreadsheet.worksheets()
    except gspread.exceptions.GSpreadException:
        raise
    return {worksheet.title for worksheet in worksheets}


def sheet_exists(sheet_name: str) -> bool:
    """Check whether a worksheet with the given name exists."""
    if not sheet_name:
        return False
    spreadsheet = _open_spreadsheet()
    try:
        spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return False
    return True


def get_current_worksheet() -> gspread.Worksheet:
    settings = load_settings()
    sheet_name = settings.get("current_event_sheet_name")
    if not sheet_name:
        raise RuntimeError("Активный лист события не настроен")
    worksheet = get_sheet_by_name(sheet_name)
    if worksheet is None:
        worksheet = create_event_sheet(sheet_name)
    return worksheet


def _now_timestamp() -> str:
    """Return the current timestamp formatted for sheet entries."""

    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")


def _find_row_by_chat_id(
    worksheet: gspread.Worksheet,
    chat_id: int,
    header_map: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    chat_id_str = str(chat_id)
    if header_map is None:
        header_map = _header_map(worksheet)
    chat_col = header_map.get("chat_id")
    if not chat_col:
        return None
    values = worksheet.col_values(chat_col)
    for idx, value in enumerate(values, start=1):
        if value.strip() == chat_id_str:
            return idx
    return None


def _normalize_username(username: str) -> str:
    username = username.strip()
    if username and not username.startswith("@"):
        return f"@{username}"
    return username


def register_participant(participant: Participant) -> None:
    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    row_idx = _find_row_by_chat_id(worksheet, participant.chat_id, header_map)
    timestamp = _now_timestamp()
    username = _normalize_username(participant.username)
    data = {header: "" for header in HEADERS}
    if row_idx:
        data.update(_row_dict(worksheet, row_idx, header_map))

    data.update(
        {
            "Время регистрации": timestamp,
            "chat_id": str(participant.chat_id),
            "Имя пользователя": username,
            "Имя": participant.name,
            "Email": participant.email,
        }
    )

    role_value = data.get("Тип участия") or participant.role
    paid_value = data.get("Статус оплаты") or participant.paid
    feedback_value = data.get("Обратная связь") or participant.feedback

    data["Тип участия"] = _format_role(role_value)
    data["Статус оплаты"] = _format_payment_status(paid_value)
    data["Обратная связь"] = feedback_value

    row_values = [data.get(header, "") for header in HEADERS]

    if row_idx:
        start = rowcol_to_a1(row_idx, 1)
        end = rowcol_to_a1(row_idx, len(HEADERS))
        worksheet.update(f"{start}:{end}", [row_values])
    else:
        worksheet.append_row(row_values)


def get_participant(chat_id: int) -> Optional[Dict[str, str]]:
    """Return participant row mapped by headers or ``None`` if missing."""

    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    row_idx = _find_row_by_chat_id(worksheet, chat_id, header_map)
    if not row_idx:
        return None
    return _row_dict(worksheet, row_idx, header_map)


def unregister_participant(chat_id: int) -> bool:
    """Remove participant from the current worksheet."""

    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    row_idx = _find_row_by_chat_id(worksheet, chat_id, header_map)
    if not row_idx:
        return False
    worksheet.delete_rows(row_idx)
    return True


def update_participation(chat_id: int, role: str, paid: str) -> None:
    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    row_idx = _find_row_by_chat_id(worksheet, chat_id, header_map)
    if not row_idx:
        return
    role_value = _format_role(role)
    paid_value = _format_payment_status(paid)
    role_col = header_map.get("Тип участия")
    paid_col = header_map.get("Статус оплаты")
    if not role_col or not paid_col:
        return
    start_col = min(role_col, paid_col)
    end_col = max(role_col, paid_col)
    start = rowcol_to_a1(row_idx, start_col)
    end = rowcol_to_a1(row_idx, end_col)
    if role_col <= paid_col:
        values = [[role_value, paid_value]]
    else:
        values = [[paid_value, role_value]]
    worksheet.update(f"{start}:{end}", values)


def update_feedback(chat_id: int, feedback: str) -> None:
    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    row_idx = _find_row_by_chat_id(worksheet, chat_id, header_map)
    if not row_idx:
        return
    feedback_col = header_map.get("Обратная связь")
    if not feedback_col:
        return
    cell = rowcol_to_a1(row_idx, feedback_col)
    worksheet.update(cell, feedback)


def get_participants(sheet_name: Optional[str] = None) -> pd.DataFrame:
    worksheet = get_current_worksheet() if sheet_name is None else get_or_create_sheet(sheet_name)
    records = worksheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=HEADERS)
    return pd.DataFrame(records, columns=HEADERS)


def list_chat_ids() -> List[int]:
    worksheet = get_current_worksheet()
    header_map = _header_map(worksheet)
    chat_col = header_map.get("chat_id")
    if not chat_col:
        return []
    values = worksheet.col_values(chat_col)
    chat_ids: List[int] = []
    for value in values[1:]:  # skip header
        value = value.strip()
        if not value:
            continue
        try:
            chat_ids.append(int(float(value)))
        except ValueError:
            continue
    return chat_ids


def export_database(destination: Optional[Path] = None) -> Path:
    """Create an XLSX export of the current participant list."""
    df = get_participants()
    if destination is None:
        destination = DATA_DIR / "participants.xlsx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(destination, index=False)
    return destination


def export_database_csv(destination: Optional[Path] = None) -> Path:
    """Create a CSV export of the current participant list."""

    df = get_participants()
    if destination is None:
        destination = DATA_DIR / "participants.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destination, index=False)
    return destination


def get_sheet_link(sheet_name: Optional[str] = None, gid: Optional[int] = None) -> str:
    if sheet_name is None:
        settings = load_settings()
        sheet_name = settings.get("current_event_sheet_name")
        gid = settings.get("current_event_sheet_gid")
    if not sheet_name:
        raise RuntimeError("Активный лист события не настроен")
    if gid is None:
        worksheet = get_sheet_by_name(sheet_name)
        if worksheet is None:
            worksheet = create_event_sheet(sheet_name)
        gid = worksheet.id
    return f"https://docs.google.com/spreadsheets/d/{GSPREAD_SHEET_ID}/edit#gid={gid}"


def get_or_create_sheet(sheet_name: str) -> gspread.Worksheet:
    worksheet = get_sheet_by_name(sheet_name)
    if worksheet is None:
        worksheet = create_event_sheet(sheet_name)
    return worksheet

