"""Google Sheets backed storage for webinar participants."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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


SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

HEADERS = [
    "timestamp",
    "chat_id",
    "username",
    "name",
    "email",
    "role",
    "paid",
    "feedback",
]

TZ = ZoneInfo(TIMEZONE)

_client: gspread.Client | None = None


@dataclass
class Participant:
    name: str
    username: str
    chat_id: int
    email: str
    role: str = "free"
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


def _ensure_headers(worksheet: gspread.Worksheet) -> None:
    current_headers = worksheet.row_values(1)
    if [h.strip() for h in current_headers] == HEADERS:
        return
    worksheet.update("1:1", [HEADERS])


def create_event_sheet(sheet_name: str) -> gspread.Worksheet:
    spreadsheet = _open_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=len(HEADERS))
        worksheet.update("1:1", [HEADERS])
    else:
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
    return datetime.now(TZ).isoformat()


def _find_row_by_chat_id(worksheet: gspread.Worksheet, chat_id: int) -> Optional[int]:
    chat_id_str = str(chat_id)
    values = worksheet.col_values(2)
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
    row_idx = _find_row_by_chat_id(worksheet, participant.chat_id)
    timestamp = _now_timestamp()
    username = _normalize_username(participant.username)
    if row_idx:
        existing = worksheet.row_values(row_idx)
        role = existing[5] if len(existing) > 5 else participant.role
        paid = existing[6] if len(existing) > 6 else participant.paid
        feedback = existing[7] if len(existing) > 7 else participant.feedback
        worksheet.update(
            f"A{row_idx}:H{row_idx}",
            [
                [
                    timestamp,
                    str(participant.chat_id),
                    username,
                    participant.name,
                    participant.email,
                    role or participant.role,
                    paid or participant.paid,
                    feedback or participant.feedback,
                ]
            ],
        )
    else:
        worksheet.append_row(
            [
                timestamp,
                str(participant.chat_id),
                username,
                participant.name,
                participant.email,
                participant.role,
                participant.paid,
                participant.feedback,
            ]
        )


def update_participation(chat_id: int, role: str, paid: str) -> None:
    worksheet = get_current_worksheet()
    row_idx = _find_row_by_chat_id(worksheet, chat_id)
    if not row_idx:
        return
    worksheet.update(f"F{row_idx}:G{row_idx}", [[role, paid]])


def update_feedback(chat_id: int, feedback: str) -> None:
    worksheet = get_current_worksheet()
    row_idx = _find_row_by_chat_id(worksheet, chat_id)
    if not row_idx:
        return
    worksheet.update(f"H{row_idx}", feedback)


def get_participants(sheet_name: Optional[str] = None) -> pd.DataFrame:
    worksheet = get_current_worksheet() if sheet_name is None else get_or_create_sheet(sheet_name)
    records = worksheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=HEADERS)
    return pd.DataFrame(records, columns=HEADERS)


def list_chat_ids() -> List[int]:
    worksheet = get_current_worksheet()
    values = worksheet.col_values(2)
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

