"""PostgreSQL-backed object store — the multi-tenant local-state backend.

Implements the same synchronous object-store contract as
:class:`~teamEvolver.storage.local.LocalObjectStore` and
:class:`~teamEvolver.storage.viking.OpenVikingObjectStore`
(``get_object`` / ``put_object`` / ``delete_object`` / ``iter_objects``) on
top of the ``objects`` kv table , with native
transactional ``batch_write`` including precondition enforcement
(``create_if_absent`` / ``replace_if_hash``).

Tenancy: every row is scoped by ``tenant_id`` (≡ the OpenViking account id);
row visibility is enforced by the schema's RLS policy plus the
``app.tenant_id`` session variable applied by
:meth:`PgRuntime.tenant_conn <teamEvolver.storage.pg_pool.PgRuntime.tenant_conn>`.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Mapping
from typing import Any, Iterator

from ..database.pool import (
    _DEFAULT_COMMAND_TIMEOUT,
    _DEFAULT_POOL_MAX,
    _DEFAULT_POOL_MIN,
    DEFAULT_SCHEMA,
    dsn_from_env,
    get_database_pool,
    validate_schema_name,
)
from .base import ObjectInfo, _BytesObject, read_bytes

_BATCH_MAX_OPERATIONS = 256
_BATCH_MAX_FILE_BYTES = 8 * 1024 * 1024
_BATCH_MAX_TOTAL_BYTES = 16 * 1024 * 1024

_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_SCHEMA = DEFAULT_SCHEMA


def _advisory_key(schema, tenant_id):
    return int.from_bytes(hashlib.sha256(f"{schema}:{tenant_id}".encode()).digest()[:8], "big", signed=True)


def validate_tenant_id(tenant_id: str) -> str:
    """Tenant ids become key-scope values; keep them conservative."""
    value = str(tenant_id or "").strip()
    if not _TENANT_ID_RE.match(value):
        raise ValueError(f"invalid tenant id: {tenant_id!r}")
    return value


def _clean_key(key: str) -> str:
    # Normalize like LocalObjectStore._resolve: collapse empty/'.' segments and
    # reject '..' so keys round-trip identically between local and PG backends.
    raw = str(key or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("PgObjectStore: empty key")
    if any(part == ".." for part in parts):
        raise ValueError(f"PgObjectStore: key escapes root: {key!r}")
    return "/".join(parts)


def _batch_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class PgObjectStore:
    """Sync object-store facade over the shared ``teamevolver`` schema."""

    native_batch_write = True

    def __init__(
        self,
        *,
        dsn: str = "",
        schema: str = _DEFAULT_SCHEMA,
        tenant_id: str = "default",
        pool_min: int = _DEFAULT_POOL_MIN,
        pool_max: int = _DEFAULT_POOL_MAX,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        ssl: str = "prefer",
        op_timeout: float = 60.0,
        runtime: Any | None = None,
    ) -> None:
        self._dsn = str(dsn or "").strip() or dsn_from_env()
        if not self._dsn:
            raise ValueError(
                "PgObjectStore requires a DSN (storage_pg.dsn or "
                "TEAMEVOLVER_PG_* env vars)"
            )
        self._schema = validate_schema_name(schema)
        self._tenant_id = validate_tenant_id(tenant_id)
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._command_timeout = command_timeout
        self._ssl = ssl or "prefer"
        self._op_timeout = float(op_timeout)
        self._runtime = runtime or get_database_pool(
            dsn=self._dsn,
            schema=schema,
            pool_min=pool_min,
            pool_max=pool_max,
            command_timeout=command_timeout,
            ssl=self._ssl,
        )

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def pool_status(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """PG health + pool metrics for ``/storage/status`` (never raises)."""
        status = self._runtime.pool_status(timeout=timeout)
        status["tenant_id"] = self._tenant_id
        return status

    # -- core contract ------------------------------------------------------ #

    def get_object(self, key: str) -> _BytesObject:
        clean = _clean_key(key)

        async def _get() -> Any:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                return await conn.fetchrow(
                    f"SELECT content FROM {self._schema}.objects "
                    "WHERE tenant_id = $1 AND key = $2",
                    self._tenant_id,
                    clean,
                )

        row = self._runtime.run(_get(), timeout=self._op_timeout)
        if row is None:
            raise FileNotFoundError(f"PgObjectStore: key not found: {key}")
        return _BytesObject(bytes(row["content"]), clean)

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        clean = _clean_key(key)
        body = read_bytes(data)

        async def _put() -> None:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                await conn.execute(
                    f"INSERT INTO {self._schema}.objects (tenant_id, key, content) "
                    "VALUES ($1, $2, $3) "
                    "ON CONFLICT (tenant_id, key) "
                    "DO UPDATE SET content = EXCLUDED.content, updated_at = now()",
                    self._tenant_id,
                    clean,
                    body,
                )

        self._runtime.run(_put(), timeout=self._op_timeout)

    def delete_object(self, key: str) -> None:
        clean = _clean_key(key)

        async def _delete() -> None:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                await conn.execute(
                    f"DELETE FROM {self._schema}.objects "
                    "WHERE tenant_id = $1 AND key = $2",
                    self._tenant_id,
                    clean,
                )

        self._runtime.run(_delete(), timeout=self._op_timeout)

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        clean_prefix = str(prefix or "").replace("\\", "/").lstrip("/")

        async def _iter() -> list[str]:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                rows = await conn.fetch(
                    f"SELECT key FROM {self._schema}.objects "
                    "WHERE tenant_id = $1 AND starts_with(key, $2) "
                    "ORDER BY key",
                    self._tenant_id,
                    clean_prefix,
                )
            return [row["key"] for row in rows]

        keys = self._runtime.run(_iter(), timeout=self._op_timeout)
        return iter(ObjectInfo(key) for key in keys)

    def iter_objects_bulk(self, prefix: str = "") -> dict[str, bytes]:
        """Return all objects under *prefix* as ``{key: content}`` in one query.

        Unlike :meth:`iter_objects` (keys only) + per-key :meth:`get_object`
        (N+1 queries), this fetches key + content in a single SQL round-trip —
        critical for remote PG where each query costs ~125 ms.
        """
        clean_prefix = str(prefix or "").replace("\\", "/").lstrip("/")

        async def _bulk() -> dict[str, bytes]:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                rows = await conn.fetch(
                    f"SELECT key, content FROM {self._schema}.objects "
                    "WHERE tenant_id = $1 AND starts_with(key, $2) "
                    "ORDER BY key",
                    self._tenant_id,
                    clean_prefix,
                )
            return {row["key"]: bytes(row["content"]) for row in rows}

        return self._runtime.run(_bulk(), timeout=self._op_timeout)

    # -- conditional batch write ------------------------------------------- #

    def changed_objects_page(self, *, pattern: str, after_time: str, after_key: str,
                             until: str, limit: int = 100) -> list[dict]:
        """Bounded timestamp/key scan; large payloads are reported, never loaded.

        A consumer must persist work before advancing its cursor and periodically
        rescan: updated_at is a transaction timestamp, not a commit sequence.
        """
        async def scan():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                rows = await conn.fetch(
                    f"SELECT key, updated_at, octet_length(content) AS size, "
                    f"CASE WHEN octet_length(content)<=1048576 THEN content END AS content "
                    f"FROM {self._schema}.objects WHERE tenant_id=$1 AND key ~ $2 "
                    "AND (updated_at, key)>($3::text::timestamptz,$4) "
                    "AND updated_at<=$5::text::timestamptz ORDER BY updated_at,key LIMIT $6",
                    self._tenant_id, pattern, after_time, after_key, until, max(1, min(100, limit)),
                )
                return [{**dict(r), "updated_at": r["updated_at"].isoformat()} for r in rows]
        return self._runtime.run(scan(), timeout=self._op_timeout)

    def object_page(self, *, prefix: str, after_key: str = "", limit: int = 100) -> list[dict]:
        """Key-ordered page, stable while a consumer updates returned objects."""
        async def scan():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                rows = await conn.fetch(
                    f"SELECT key, content FROM {self._schema}.objects "
                    "WHERE tenant_id=$1 AND starts_with(key,$2) AND key>$3 ORDER BY key LIMIT $4",
                    self._tenant_id, prefix, after_key, max(1, min(100, limit)),
                )
                return [dict(row) for row in rows]
        return self._runtime.run(scan(), timeout=self._op_timeout)

    def database_time(self) -> str:
        async def now():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                return (await conn.fetchval("SELECT clock_timestamp()")).isoformat()
        return self._runtime.run(now(), timeout=self._op_timeout)




    def object_precondition(self, key: str) -> dict[str, str]:
        """Capture the current object state for a later conditional batch."""
        clean = _clean_key(key)
        try:
            current = self.get_object(clean).read()
        except FileNotFoundError:
            return {"kind": "create_if_absent"}
        return {"kind": "replace_if_hash", "base_hash": _batch_hash(current)}

    def batch_write(
        self,
        objects: Mapping[str, bytes | str | io.IOBase],
        *,
        preconditions: Mapping[str, Mapping[str, str]] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Write one all-or-nothing batch under this tenant's scope.

        Preconditions are checked inside the same transaction as the writes:
        any violation aborts the whole batch and raises ``RuntimeError``,
        matching the OpenViking store's conflict semantics.
        """
        if not objects:
            raise ValueError("batch_write requires at least one object")
        if len(objects) > _BATCH_MAX_OPERATIONS:
            raise ValueError(
                f"batch_write supports at most {_BATCH_MAX_OPERATIONS} objects"
            )

        prepared: dict[str, bytes] = {
            _clean_key(key): read_bytes(value) for key, value in objects.items()
        }
        conditions = {_clean_key(key): value for key, value in (preconditions or {}).items()}
        for condition in conditions.values():
            if condition.get("kind") not in {"create_if_absent", "replace_if_hash"}:
                raise ValueError("unsupported batch_write precondition")
        oversized = [
            key
            for key, value in prepared.items()
            if len(value) > _BATCH_MAX_FILE_BYTES
        ]
        if oversized:
            raise ValueError(f"batch_write object exceeds 8 MiB: {oversized[0]}")
        if sum(len(v) for v in prepared.values()) > _BATCH_MAX_TOTAL_BYTES:
            raise ValueError("batch_write total content exceeds 16 MiB")

        async def _batch() -> dict[str, Any]:
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                async with conn.transaction():
                    for key, body in sorted(prepared.items()):
                        row = await conn.fetchrow(
                            f"SELECT content FROM {self._schema}.objects "
                            "WHERE tenant_id = $1 AND key = $2 FOR UPDATE",
                            self._tenant_id,
                            key,
                        )
                        condition = conditions.get(key) or {}
                        kind = str(condition.get("kind") or "")
                        if kind == "create_if_absent" and row is not None:
                            raise RuntimeError(
                                f"batch_write precondition conflict: {key} "
                                "(create_if_absent but object exists)"
                            )
                        if kind == "replace_if_hash":
                            base_hash = str(condition.get("base_hash") or "")
                            current_hash = (
                                _batch_hash(bytes(row["content"]))
                                if row is not None
                                else ""
                            )
                            if row is None or current_hash != base_hash:
                                raise RuntimeError(
                                    f"batch_write precondition conflict: {key} "
                                    "(replace_if_hash mismatch)"
                                )
                        # An absent row cannot be protected by SELECT FOR UPDATE.
                        # INSERT... DO NOTHING decides the winner atomically.
                        if kind == "create_if_absent":
                            result = await conn.execute(
                                f"INSERT INTO {self._schema}.objects (tenant_id,key,content) VALUES ($1,$2,$3) "
                                "ON CONFLICT (tenant_id,key) DO NOTHING",
                                self._tenant_id, key, body,
                            )
                            if result == "INSERT 0 0":
                                raise RuntimeError(f"batch_write precondition conflict: {key} (create_if_absent)")
                            continue
                        await conn.execute(
                            f"INSERT INTO {self._schema}.objects "
                            "(tenant_id, key, content) VALUES ($1, $2, $3) "
                            "ON CONFLICT (tenant_id, key) "
                            "DO UPDATE SET content = EXCLUDED.content, "
                            "updated_at = now()",
                            self._tenant_id,
                            key,
                            body,
                        )
            return {
                "succeeded": sorted(prepared.keys()),
                "failed": [],
                "mode": "transactional",
            }

        return self._runtime.run(_batch(), timeout=timeout or self._op_timeout)

    # -- lifecycle ----------------------------------------------------------- #

    def close(self) -> None:
        """Close this store's runtime pool (tests / process shutdown)."""
        self._runtime.close()
