"""General utility helpers for the bot."""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Dict, MutableMapping, Optional, cast


def make_event_key(event_id: str) -> str:
    """Return a stable short key for a given event identifier."""
    h = hashlib.sha256(event_id.encode("utf-8")).digest()[:12]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")


def _event_key_map(bot_data: MutableMapping[str, Any]) -> MutableMapping[str, str]:
    mapping = bot_data.setdefault("ev_by_key", {})
    if not isinstance(mapping, dict):
        mapping = {}
        bot_data["ev_by_key"] = mapping
    return cast(Dict[str, str], mapping)


def map_event_key(context: Any, key: str, event_id: str) -> None:
    """Associate an event key with its full identifier inside bot data."""
    if not context or not getattr(context, "bot_data", None):
        return
    mapping = _event_key_map(context.bot_data)
    mapping[key] = event_id


def resolve_event_id(context: Any, key: str) -> Optional[str]:
    """Resolve an event identifier by its short key from bot data."""
    if not context or not getattr(context, "bot_data", None):
        return None
    mapping = context.bot_data.get("ev_by_key")
    if isinstance(mapping, dict):
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return None
