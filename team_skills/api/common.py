"""Bounded JSON request reader for Skills command APIs."""
import json

from fastapi import Request
from starlette.exceptions import HTTPException


async def read_json(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 8 * 1024 * 1024:
            raise HTTPException(413, "command body exceeds 8 MiB")
        raw.extend(chunk)
    try:
        body = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HTTPException(400, "command body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "command body must be an object")
    return body
