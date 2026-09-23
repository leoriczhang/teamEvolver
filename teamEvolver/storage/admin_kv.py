"""Tenant-scoped admin state in PG or files, with no cross-backend fallback."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..tenants.registry import DEFAULT_TENANT_ID, current_tenant_id
from .pg_store import PgObjectStore

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
        raise RuntimeError("admin state PG backend requires a DSN")
    return PgObjectStore(
            dsn=dsn,
            schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            pool_min=max(1, int(getattr(config, "storage_pg_pool_min", 2) or 2)),
            pool_max=max(2, int(getattr(config, "storage_pg_pool_max", 20) or 20)),
            command_timeout=float(
                getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
            ),
            ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
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
    """Missing state is empty; corrupt or unreadable state must fail closed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("admin state must be an object")
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise RuntimeError(f"admin state unavailable: {path.name}") from exc


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
    """Read the configured backend; a missing record returns an empty dict."""
    tid = _resolve_tenant(config, tenant_id)
    store = _pg_store(config, tid)

    # Primary: PG.
    if store is not None:
        try:
            raw = store.get_object(kv_key(name)).read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("admin state must be an object")
            return data
        except FileNotFoundError:
            # Never seed a new account from another account's local cache.
            return {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"admin state unavailable: {name}") from exc

    # Files are authoritative only when PG is disabled.
    data = _file_read(file_path)
    if data is not None:
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
    """Write only to the configured scope, propagating persistence failures."""
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

    # File mode uses an atomic, durable write.
    try:
        _file_write(file_path, data)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"admin state write failed: {name}") from exc
