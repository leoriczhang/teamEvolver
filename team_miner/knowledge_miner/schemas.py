"""Miner upload and URI limits, checked before conversion or external writes."""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import unquote

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 200


def relative_path(value):
    raw = str(value or "")
    if raw != unquote(raw) or raw.startswith("/") or "\\" in raw or ":" in raw or len(raw) > 500:
        raise ValueError("invalid upload path")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in raw.split("/")):
        raise ValueError("invalid upload path")
    return raw


def uri(value, root, *, allow_root=True):
    raw = str(value or root).rstrip("/")
    if raw != unquote(raw) or "\\" in raw or len(raw) > 1000:
        raise ValueError("invalid knowledge URI")
    if raw != root and not raw.startswith(root + "/"):
        raise ValueError("knowledge URI escapes workspace")
    if raw == root and not allow_root:
        raise ValueError("workspace root cannot be modified")
    tail = raw[len(root):].lstrip("/")
    if tail and any(part in {"", ".", ".."} or part.startswith(".") for part in tail.split("/")):
        raise ValueError("invalid knowledge URI")
    return raw


def task_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ValueError("invalid task ID")
    return value


def uploads(raw):
    files = raw.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("upload requires 1..200 files")
    result, total = [], 0
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("upload file must be an object")
        path = relative_path(item.get("path") or item.get("name"))
        target = path.rsplit(".", 1)[0] + ".md"
        if target in seen:
            raise ValueError("upload files have the same Markdown destination")
        seen.add(target)
        encoded = item.get("content_b64", item.get("content_base64", ""))
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES * 4 // 3) + 8:
            raise ValueError("upload file exceeds 20 MiB")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64 upload") from exc
        total += len(content)
        if not content or len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("upload exceeds size budget")
        result.append((path, content))
    return result
