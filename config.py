"""Configuration utilities for the psychology webinar bot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

# Bot credentials and application level constants
BOT_TOKEN: str = "8334419696:AAFrH3-0nUn518KogBviZ-Qquvl6QtHwyDU"
# Replace with the administrator chat ID that should have access to privileged commands.
ADMIN_CHAT_ID: int = 123456789

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
