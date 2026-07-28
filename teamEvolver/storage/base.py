"""Object-store primitives and helpers shared by all backends.

Two deployment modes are supported by the concrete stores in this package:

- ``local``: a local filesystem directory tree (development / unit tests).
- ``viking``: an OpenViking account-scoped *resources* namespace. Every object
  (skills, manifest, registry, sessions) lives under the team-shared root
  ``viking://resources/{root_prefix}/...`` (an optional ``{group_id}`` segment
  may follow ``{root_prefix}`` for isolation, but the team library uses none).
  This is the same namespace Hermes' ``OpenVikingSkillSource`` reads team
  skills from, so pushed skills become installable without any mirroring.
  Per-person isolation is layered on top by callers via ``peers/{customer_id}/``
  key prefixes (see :func:`peer_key_prefix`).
"""

from __future__ import annotations

import io


class ObjectInfo:
    """Lightweight object listing entry with a single ``key`` field."""

    def __init__(self, key: str) -> None:
        self.key = key


class _BytesObject:
    """Simple in-memory object body that exposes ``read()``."""

    def __init__(self, data: bytes, key: str) -> None:
        self._data = data
        self.key = key

    def read(self) -> bytes:
        return self._data


def read_bytes(data: bytes | str | io.IOBase) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    return data.read()


def normalize_backend(backend: str | None, *, endpoint: str = "", local_root: str = "") -> str:
    """Map user-facing aliases into the concrete backend names we support."""
    value = str(backend or "").strip().lower().replace("_", "-")
    aliases = {
        "filesystem": "local",
        "fs": "local",
        "openviking": "viking",
        "open-viking": "viking",
    }
    if value in aliases:
        return aliases[value]
    if value:
        return value
    if local_root:
        return "local"
    return ""


def peer_key_prefix(customer_id: str) -> str:
    """Return the object-store key prefix for per-customer (isolated) data.

    Agent-level (shared) artifacts use a bare key (e.g. ``skills/...``). Data
    scoped to a single end-customer is stored under
    ``peers/{customer_id}/...`` so it is isolated from other customers while
    living inside the same per-Agent namespace.
    """
    cid = str(customer_id or "").strip().strip("/")
    return f"peers/{cid}/" if cid else ""


def is_not_found_error(exc: Exception) -> bool:
    """Best-effort check for backends that signal missing objects differently."""
    if isinstance(exc, FileNotFoundError):
        return True
    name = type(exc).__name__
    text = str(exc)
    if "NotFound" in name:
        return True
    # OpenViking surfaces missing URIs as "NOT_FOUND: ..." or "RESOURCE_NOT_FOUND: ..."
    if "NOT_FOUND" in text or "NoSuchURI" in text:
        return True
    return False
