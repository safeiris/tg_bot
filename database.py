"""Simple Excel backed storage for webinar participants."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pandas as pd

from config import DATA_DIR

DATABASE_PATH = DATA_DIR / "participants.xlsx"
COLUMNS = [
    "Имя",
    "Username",
    "ChatID",
    "Email",
    "Формат",
    "Оплата",
    "Фидбэк",
]


@dataclass
class Participant:
    name: str
    username: str
    chat_id: int
    email: str
    participation_type: str = "free"
    payment_status: str = "нет"
    feedback: str = ""


def _load_dataframe() -> pd.DataFrame:
    if not DATABASE_PATH.exists():
        ensure_database()
    return pd.read_excel(DATABASE_PATH)


def ensure_database() -> None:
    """Create an empty database file if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists():
        return
    empty = pd.DataFrame(columns=COLUMNS)
    empty.to_excel(DATABASE_PATH, index=False)


def add_or_update_participant(participant: Participant) -> None:
    df = _load_dataframe()
    mask = df["ChatID"] == participant.chat_id
    if mask.any():
        df.loc[mask, ["Имя", "Username", "Email"]] = [
            participant.name,
            participant.username,
            participant.email,
        ]
    else:
        df = pd.concat(
            [
                df,
                pd.DataFrame(
                    [
                        [
                            participant.name,
                            participant.username,
                            participant.chat_id,
                            participant.email,
                            participant.participation_type,
                            participant.payment_status,
                            participant.feedback,
                        ]
                    ],
                    columns=COLUMNS,
                ),
            ],
            ignore_index=True,
        )
    df.to_excel(DATABASE_PATH, index=False)


def update_participation(chat_id: int, participation_type: str, payment_status: str) -> None:
    df = _load_dataframe()
    mask = df["ChatID"] == chat_id
    if not mask.any():
        return
    df.loc[mask, ["Формат", "Оплата"]] = [participation_type, payment_status]
    df.to_excel(DATABASE_PATH, index=False)


def update_feedback(chat_id: int, feedback: str) -> None:
    df = _load_dataframe()
    mask = df["ChatID"] == chat_id
    if not mask.any():
        return
    df.loc[mask, "Фидбэк"] = feedback
    df.to_excel(DATABASE_PATH, index=False)


def get_participants() -> pd.DataFrame:
    return _load_dataframe()


def list_chat_ids() -> List[int]:
    df = _load_dataframe()
    return [int(cid) for cid in df["ChatID"].dropna().tolist()]


def export_database(destination: Optional[Path] = None) -> Path:
    """Create a copy of the database and return the file path."""
    ensure_database()
    if destination is None:
        destination = DATABASE_PATH
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        df = _load_dataframe()
        df.to_excel(destination, index=False)
        return destination
    return destination
