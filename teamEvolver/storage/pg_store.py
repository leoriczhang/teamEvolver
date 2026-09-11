"""PostgreSQL-backed object store — the multi-tenant local-state backend.

Implements the same synchronous object-store contract as
:class:`~teamEvolver.storage.local.LocalObjectStore` and
:class:`~teamEvolver.storage.viking.OpenVikingObjectStore`
(``get_object`` / ``put_object`` / ``delete_object`` / ``iter_objects``) on
top of the ``objects`` kv table (multi-tenancy plan §2.1), with native
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
import json
import re
from collections.abc import Mapping
from typing import Any, Iterator

from .base import ObjectInfo, _BytesObject, read_bytes
from .pg_pool import (
    _DEFAULT_COMMAND_TIMEOUT,
    _DEFAULT_POOL_MAX,
    _DEFAULT_POOL_MIN,
    _DEFAULT_SCHEMA,
    _advisory_key,
    dsn_from_env,
    get_pg_runtime,
    validate_schema_name,
)

_BATCH_MAX_OPERATIONS = 256
_BATCH_MAX_FILE_BYTES = 8 * 1024 * 1024
_BATCH_MAX_TOTAL_BYTES = 16 * 1024 * 1024

_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
        op_timeout: float = 60.0,
        runtime: Any | None = None,
    ) -> None:
        self._dsn = str(dsn or "").strip() or dsn_from_env()
        if not self._dsn:
            raise ValueError(
                "PgObjectStore requires a DSN (storage_pg.dsn or OV_PG_* env vars)"
            )
        self._schema = validate_schema_name(schema)
        self._tenant_id = validate_tenant_id(tenant_id)
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._command_timeout = command_timeout
        self._op_timeout = float(op_timeout)
        self._runtime = runtime or get_pg_runtime(
            dsn=self._dsn,
            schema=schema,
            pool_min=pool_min,
            pool_max=pool_max,
            command_timeout=command_timeout,
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

    def write_session_records(self, objects: Mapping[str, bytes], index_key: str, rows: list[dict]) -> None:
        """Commit queue/archive/audit and per-session index rows atomically."""
        async def write():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                async with conn.transaction():
                    for row in sorted(rows, key=lambda value: value["session_id"]):
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock($1)",
                            _advisory_key(self._schema, f"{self._tenant_id}:session:{row['session_id']}"),
                        )
                    for key, body in sorted(objects.items()):
                        await conn.execute(
                            f"INSERT INTO {self._schema}.objects (tenant_id, key, content) VALUES ($1,$2,$3) "
                            "ON CONFLICT (tenant_id,key) DO UPDATE SET content=EXCLUDED.content, updated_at=now()",
                            self._tenant_id, _clean_key(key), body,
                        )
                    for row in rows:
                        await conn.execute(
                            f"INSERT INTO {self._schema}.session_index (tenant_id,index_key,session_id,meta) "
                            "VALUES ($1,$2,$3,$4::jsonb) ON CONFLICT (tenant_id,index_key,session_id) "
                            "DO UPDATE SET meta=EXCLUDED.meta, updated_at=now()",
                            self._tenant_id, index_key, row["session_id"], json.dumps(row),
                        )
        self._runtime.run(write(), timeout=self._op_timeout)

    def consume_session(self, queue_key: str, expected_hash: str, archive_key: str, archive: dict) -> bool:
        """Acknowledge only the exact Session revision consumed by this cycle."""
        if not expected_hash:
            raise ValueError("session consumption requires the drained content hash")

        async def consume():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1)",
                        _advisory_key(self._schema, f"{self._tenant_id}:session:{archive['session_id']}"),
                    )
                    row = await conn.fetchrow(
                        f"SELECT content FROM {self._schema}.objects WHERE tenant_id=$1 AND key=$2 FOR UPDATE",
                        self._tenant_id, queue_key,
                    )
                    if row is None or hashlib.sha256(bytes(row["content"])).hexdigest() != expected_hash:
                        return False
                    await conn.execute(
                        f"INSERT INTO {self._schema}.objects (tenant_id,key,content) VALUES ($1,$2,$3) "
                        "ON CONFLICT (tenant_id,key) DO UPDATE SET content=EXCLUDED.content, updated_at=now()",
                        self._tenant_id, archive_key, json.dumps(archive).encode(),
                    )
                    await conn.execute(
                        f"DELETE FROM {self._schema}.objects WHERE tenant_id=$1 AND key=$2",
                        self._tenant_id, queue_key,
                    )
                    return True
        return self._runtime.run(consume(), timeout=self._op_timeout)

    def read_session_index(self, index_key: str, limit: int = 10000) -> list[dict]:
        async def read():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                return await conn.fetch(
                    f"SELECT meta FROM {self._schema}.session_index "
                    "WHERE tenant_id=$1 AND index_key=$2 ORDER BY updated_at DESC LIMIT $3",
                    self._tenant_id, index_key, limit,
                )
        rows = self._runtime.run(read(), timeout=self._op_timeout)
        return [json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"] for row in rows]

    # -- conditional batch write ------------------------------------------- #

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
        wait: bool = True,
        timeout: float | None = None,
        telemetry: bool = True,
        default_mode: str = "upsert",
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
                        # INSERT ... DO NOTHING decides the winner atomically.
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
