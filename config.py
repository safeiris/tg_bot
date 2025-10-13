"""Configuration utilities for the psychology webinar bot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Bot credentials and application level constants
BOT_TOKEN: str = "8439661494:AAG5nqW5raGjVjocSX6L8oCS1hZngqdq-Uo"

# Administrators who can access privileged commands.
ADMINS: Tuple[Dict[str, Any], ...] = (
    {
        "chat_id": 7740254761,
        "username": "z_ivan89",
    },
)


# === Google Sheets Integration ===
GSPREAD_CREDENTIALS_PATH = "./service_account.json"
GSPREAD_SHEET_ID = "1f5bRTFlKQ3FD-u0cggKj-87HdXNzMwz_H8imefFaNmI"
TIMEZONE = "Europe/Moscow"



# Storage locations
DATA_DIR = Path("data")
SETTINGS_FILE = DATA_DIR / "config.json"

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "topic": "Психологический вебинар",
    "description": "Авторский вебинар по психологии",
    "event_datetime": None,  # ISO formatted date time string
    "zoom_link": "",
    "payment_link": "",
}


def is_admin(chat_id: Optional[int] = None, username: Optional[str] = None) -> bool:
    """Return True if provided identifiers match a known administrator."""

    normalized_username = (username or "").lstrip("@").lower()
    for admin in ADMINS:
        admin_chat_id = admin.get("chat_id")
        admin_username = (admin.get("username") or "").lstrip("@").lower()

        if chat_id is not None and admin_chat_id == chat_id:
            return True
        if normalized_username and admin_username and normalized_username == admin_username:
            return True
    return False


def ensure_data_dir() -> None:
    """Ensure that all required folders exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    """Load the admin editable settings from disk."""
    ensure_data_dir()
    if not SETTINGS_FILE.exists():
        save_settings(_DEFAULT_SETTINGS)
        return dict(_DEFAULT_SETTINGS)

    with SETTINGS_FILE.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    merged = dict(_DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist settings to disk."""
    ensure_data_dir()
    with SETTINGS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)


def update_settings(**kwargs: Any) -> Dict[str, Any]:
    """Update settings atomically with provided key-value pairs."""
    current = load_settings()
    current.update({k: v for k, v in kwargs.items() if v is not None})
    save_settings(current)
    return current
