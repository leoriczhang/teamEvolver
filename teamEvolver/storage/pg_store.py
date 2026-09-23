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
        self._runtime = runtime or get_pg_runtime(
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

    def backfill_session_index_fields(self, index_key: str, rows: list[dict]) -> None:
        """Fill missing metadata without overwriting a concurrent re-ingest."""
        async def backfill():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                async with conn.transaction():
                    for row in rows:
                        fields = {key: row[key] for key in ("used_skills", "meta", "experiences") if key in row}
                        await conn.execute(
                            f"UPDATE {self._schema}.session_index AS current SET meta = current.meta || "
                            "(SELECT COALESCE(jsonb_object_agg(field.key, field.value), '{}'::jsonb) "
                            "FROM jsonb_each($4::jsonb) AS field WHERE NOT (current.meta ? field.key)) "
                            "WHERE current.tenant_id=$1 AND current.index_key=$2 AND current.session_id=$3",
                            self._tenant_id, index_key, row["session_id"], json.dumps(fields),
                        )
        self._runtime.run(backfill(), timeout=self._op_timeout)

    def experience_import_sources(self, **kwargs):
        from .experience_import import sources_page

        return sources_page(self, **kwargs)

    def experience_import_page(self, source, **kwargs):
        from .experience_import import project_page

        return project_page(self, source, **kwargs)

    def experience_import_unchanged(self, source):
        from .experience_import import source_unchanged

        return source_unchanged(self, source)

    def session_experience_page(self, *, after_index: str = "", after_session: str = "",
                                until: str, limit: int = 100) -> list[dict]:
        """Tenant-scoped keyset scan without the UI's 10,000 Session limit."""
        async def read():
            async with self._runtime.tenant_conn(self._tenant_id) as conn:
                rows = await conn.fetch(
                    f"SELECT index_key,session_id,CASE WHEN octet_length(meta::text)<=1048576 THEN meta END AS meta "
                    f"FROM {self._schema}.session_index WHERE tenant_id=$1 "
                    "AND (index_key,session_id)>($2,$3) AND updated_at<=$4::text::timestamptz "
                    "ORDER BY index_key,session_id LIMIT $5",
                    self._tenant_id, after_index, after_session, until, max(1, min(100, limit)),
                )
                return [{**dict(row), "meta": json.loads(row["meta"]) if isinstance(row["meta"], str)
                         else row["meta"]} for row in rows]
        return self._runtime.run(read(), timeout=self._op_timeout)

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

    def try_background_lock(self, namespace: str) -> bool:
        return self._runtime.try_advisory_lock(f"{namespace}:{self._tenant_id}")

    def check_background_lock(self, namespace: str) -> None:
        """Fail closed if the dedicated lock connection was lost."""
        name = f"{namespace}:{self._tenant_id}"
        async def check():
            with self._runtime._advisory_lock:
                conn = self._runtime._advisory_conns.get(name)
            try:
                if conn is None or conn.is_closed():
                    raise RuntimeError("BACKGROUND_LOCK_LOST")
                await conn.fetchval("SELECT 1")
            except Exception:
                # A terminated asyncpg pool proxy can raise even on is_closed.
                raise RuntimeError("BACKGROUND_LOCK_LOST") from None
        self._runtime.run(check(), timeout=self._op_timeout)

    def release_background_lock(self, namespace: str) -> None:
        self._runtime.release_advisory_lock(f"{namespace}:{self._tenant_id}")

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
