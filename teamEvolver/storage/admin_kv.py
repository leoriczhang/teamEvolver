"""Write-through PG + file-fallback KV store for admin state files.

Manages small JSON state files (users.json, agents.json, prompt_overrides,
stage_settings, agent_context_state) with the following strategy:

1. **Write**: PG first (``objects`` kv table, RLS-isolated), then file
   (local cache + CLI compatibility).
2. **Read**: PG first; on miss, read file and lazily seed PG.
3. **PG unavailable**: transparently degrade to file-only (current behavior).

This module is intentionally stateless — each call builds a short-lived
``PgObjectStore`` from the config, so there's no connection lifecycle to
manage.  The ``objects`` table already exists (Phase 0 DDL) with RLS, so no
new DDL is required.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ..tenants.registry import DEFAULT_TENANT_ID, current_tenant_id
from .pg_store import PgObjectStore

logger = logging.getLogger(__name__)

_ADMIN_PREFIX = "_admin/"


def _pg_store(config: Any, tenant_id: str | None = None) -> PgObjectStore | None:
    """Build a short-lived PgObjectStore, or None when PG is not enabled."""
    if not bool(getattr(config, "storage_pg_enabled", False)):
        return None
    dsn = str(getattr(config, "storage_pg_dsn", "") or "")
    if not dsn:
        from .pg_pool import dsn_from_env

        dsn = dsn_from_env()
    if not dsn:
        return None
    return PgObjectStore(
            dsn=dsn,
            schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            pool_min=max(1, int(getattr(config, "storage_pg_pool_min", 2) or 2)),
            pool_max=max(2, int(getattr(config, "storage_pg_pool_max", 20) or 20)),
            command_timeout=float(
                getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
            ),
    )


def _resolve_tenant(config: Any, tenant_id: str | None) -> str:
    """Use the explicit tenant_id, or fall back to the request context."""
    if tenant_id:
        return tenant_id
    try:
        return current_tenant_id()
    except Exception:  # noqa: BLE001 - contextvar not set (CLI / background)
        return DEFAULT_TENANT_ID


def _file_read(path: Path) -> dict[str, Any] | None:
    """Read a JSON file; return None on missing/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _file_write(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON file with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def kv_key(name: str) -> str:
    """Build the object-store key for one admin file."""
    return f"{_ADMIN_PREFIX}{name}"


def read_kv(
    config: Any,
    name: str,
    file_path: Path,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Read an admin JSON document with PG-first, file-fallback semantics.

    Returns an empty dict when neither source has data (same as the
    pre-migration file-only behavior for a missing file).
    """
    tid = _resolve_tenant(config, tenant_id)
    store = _pg_store(config, tid)

    # Primary: PG.
    if store is not None:
        try:
            raw = store.get_object(kv_key(name)).read()
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            # Never seed a new account from another account's local cache.
            return {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"admin state unavailable: {name}") from exc

    # Fallback: file.
    data = _file_read(file_path)
    if data is not None:
        # Lazy migration: seed PG so subsequent reads hit it directly.
        if store is not None:
            try:
                store.put_object(kv_key(name), json.dumps(data, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[admin_kv] lazy seed failed for %s: %s", name, exc)
        return data

    return {}


def write_kv(
    config: Any,
    name: str,
    file_path: Path,
    data: dict[str, Any],
    *,
    tenant_id: str | None = None,
) -> None:
    """Write an admin JSON document (PG first, then file).

    File write always happens (CLI compatibility + local-backend fallback).
    PG write is best-effort: failures are logged but don't block the caller.
    """
    tid = _resolve_tenant(config, tenant_id)
    store = _pg_store(config, tid)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")

    # Primary: PG.
    if store is not None:
        try:
            store.put_object(kv_key(name), payload)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"admin state write failed: {name}") from exc
        return

    # Always write the file (CLI compat + local-backend fallback).
    try:
        _file_write(file_path, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[admin_kv] file write failed for %s: %s", name, exc)
