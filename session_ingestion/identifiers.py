"""Canonical Session identifier handling shared by push and pull transports."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


class InvalidSessionId(ValueError):
    pass


def tenant_user_session_id(tenant_id: str, user_id: str, external_id: Any) -> str:
    """Opaque v2 storage ID; avoid collisions even when sanitized labels match."""
    if not isinstance(external_id, str) or not external_id.strip():
        raise InvalidSessionId("session_id is required")
    if len(external_id) > 4096:
        raise InvalidSessionId("session_id is too long")
    key = json.dumps([tenant_id, user_id, external_id], ensure_ascii=False, separators=(",", ":"))
    return "session_v2_" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def sanitize_session_id(value: Any, *, fallback: str | None = "session") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise InvalidSessionId("session_id is required")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-/")[:160]
    if cleaned:
        return cleaned
    if fallback is not None:
        return fallback
    raise InvalidSessionId("invalid session_id")
