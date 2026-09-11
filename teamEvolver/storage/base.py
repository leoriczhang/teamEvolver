"""Object-store primitives and helpers shared by all backends.

The only supported sharing backend is ``viking`` — an OpenViking
account-scoped *resources* namespace. The same backend serves both supported
deployments: cloud OpenViking (Volcengine-hosted) and local OpenViking (a
self-hosted ``openviking-server``); they differ only by endpoint. Every object
(skills, manifest, registry, sessions) lives under the team-shared root
``viking://resources/{root_prefix}/...`` (an optional ``{group_id}`` segment
may follow ``{root_prefix}`` for isolation, but the team library uses none).
This is the same namespace Hermes' ``OpenVikingSkillSource`` reads team skills
from, so pushed skills become installable without any mirroring. Per-person
isolation is layered on top by callers via ``peers/{customer_id}/`` key
prefixes (see :func:`peer_key_prefix`).

An in-process :class:`~teamEvolver.storage.memory.InMemoryObjectStore` also
implements this contract, but it is reserved for unit tests and the evolve
engine's ``mock`` mode — it is never a user-selectable sharing backend.

A filesystem-backed :class:`~teamEvolver.storage.local.LocalObjectStore`
implements the same contract as teamEvolver's built-in storage: the automatic
fallback when the configured OpenViking deployment is unavailable.
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
    """Map user-facing aliases into the concrete backend names we support.

    ``viking`` (cloud or local OpenViking) is the primary backend; ``local``
    selects the built-in filesystem store (teamEvolver's own storage), which is
    also the automatic fallback when OpenViking is unavailable. ``local_root``
    is accepted for signature compatibility but does not by itself select the
    local backend; when a viking alias or endpoint is present the result is
    ``"viking"``, otherwise the empty string.
    """
    value = str(backend or "").strip().lower().replace("_", "-")
    local_aliases = {"local", "localfs", "local-fs", "filesystem", "fs", "builtin", "built-in"}
    if value in local_aliases:
        return "local"
    aliases = {
        "openviking": "viking",
        "open-viking": "viking",
    }
    if value in aliases:
        return "viking"
    if value == "viking":
        return "viking"
    # PostgreSQL local-state backend (multi-tenancy plan §2.4).
    if value in {"pg", "postgres", "postgresql"}:
        return "postgres"
    if value:
        # Unknown/legacy backend names collapse to the viking backend.
        return "viking"
    # No explicit backend: only a memory:// endpoint selects viking (test
    # buckets); a plain endpoint with no backend means "not configured" —
    # per-purpose callers default to the built-in local backend.
    if str(endpoint or "").strip().lower().startswith("memory://"):
        return "viking"
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
