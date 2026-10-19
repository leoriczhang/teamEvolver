"""Idempotent OpenViking directory initialization."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .client import OpenVikingError

_SCOPES = {"resources", "user"}
_EXISTING_TOKENS = ("ALREADY_EXISTS", "CONFLICT")


def directory_ancestors(uris: Iterable[str]) -> list[str]:
    """Expand resource/user URIs into a deduplicated parent-first list."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in uris:
        parts = str(raw or "").strip().rstrip("/").split("/")
        if len(parts) < 4 or parts[0] != "viking:" or parts[2] not in _SCOPES:
            raise ValueError(f"unsupported OpenViking directory URI: {raw}")
        scope_root = "/".join(parts[:3])
        for end in range(1, len(parts[3:]) + 1):
            directory = f"{scope_root}/{'/'.join(parts[3:3 + end])}"
            if directory not in seen:
                seen.add(directory)
                ordered.append(directory)
    return ordered


async def ensure_directories(client: Any, uris: Iterable[str]) -> list[str]:
    """Create missing directories and reuse existing directories unchanged."""
    directories = directory_ancestors(uris)
    for directory in directories:
        result = await client.request(
            "POST",
            "/api/v1/fs/mkdir",
            json_body={"uri": directory},
        )
        if result.ok or result.status_code == 409:
            continue
        error = str(result.error or "").upper()
        if any(token in error for token in _EXISTING_TOKENS):
            continue
        raise OpenVikingError(
            f"OpenViking HTTP {result.status_code}",
            status_code=result.status_code,
        )
    return directories


__all__ = ["directory_ancestors", "ensure_directories"]
