"""HTTP helpers shared by Session ingestion transports."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import HTTPException, Request


def max_session_body_bytes() -> int:
    try:
        value = int(
            os.environ.get(
                "TEAMEVOLVER_MAX_SESSION_BODY_BYTES",
                str(32 * 1024 * 1024),
            )
            or 0
        )
    except ValueError:
        value = 32 * 1024 * 1024
    return max(1024, value)


async def read_limited_json_body(request: Request) -> dict[str, Any]:
    limit = max_session_body_bytes()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"session body exceeds {limit} bytes",
            )
        raw.extend(chunk)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="session body must be valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="session body must be an object",
        )
    return parsed
