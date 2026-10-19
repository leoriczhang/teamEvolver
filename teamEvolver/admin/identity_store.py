"""Transactional console identity control plane, independent of tenant objects.

PostgreSQL deployments use versioned tables; isolated tests may use SQLite.
There is no runtime migration of old JSON identities or backend fallback.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import copy_context
from pathlib import Path
from typing import Callable

from starlette.exceptions import HTTPException

_TABLES = {
    "console_users": "id TEXT PRIMARY KEY, record TEXT NOT NULL",
    "tenant_memberships": (
        "tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, record TEXT NOT NULL, "
        "PRIMARY KEY (tenant_id, user_id)"
    ),
    "console_support_sessions": "id TEXT PRIMARY KEY, record TEXT NOT NULL",
    "console_login_sessions": "id TEXT PRIMARY KEY, record TEXT NOT NULL",
    "console_auth_audit": "id TEXT PRIMARY KEY, created_at DOUBLE PRECISION NOT NULL, record TEXT NOT NULL",
    "console_auth_meta": "id TEXT PRIMARY KEY, record TEXT NOT NULL",
}
_LOCAL_IO = ThreadPoolExecutor(max_workers=4, thread_name_prefix="console-identity")
_INITIALIZED: set[str] = set()
_INITIALIZED_LOCK = threading.Lock()


def registry_path(config) -> Path:
    config_file = str(getattr(config, "_config_file", "") or "")
    base = Path(config_file).expanduser().parent if config_file else Path(
        os.environ.get("TEAMEVOLVER_CONFIG_DIR") or Path.home() / ".teamEvolver"
    )
    return base / "console_identity.sqlite3"


class _SQLiteConnection:
    def __init__(self, conn):
        self.conn = conn

    def _execute(self, sql, args):
        positions = [int(match) - 1 for match in re.findall(r"\$(\d+)", sql)]
        return self.conn.execute(re.sub(r"\$\d+", "?", sql), [args[index] for index in positions])

    async def execute(self, sql, *args):
        return self._execute(sql, args)

    async def fetch(self, sql, *args):
        return [dict(row) for row in self._execute(sql, args).fetchall()]

    async def fetchrow(self, sql, *args):
        row = self._execute(sql, args).fetchone()
        return dict(row) if row else None


class IdentityStore:
    def __init__(self, config, *, runtime=None):
        self.config = config
        self.path = registry_path(config)
        self.runtime = runtime
        self.pg = bool(getattr(config, "storage_pg_enabled", False))
        if self.pg and runtime is None:
            from ..database.pool import dsn_from_env, get_database_pool

            self.runtime = get_database_pool(
                dsn=getattr(config, "storage_pg_dsn", "") or dsn_from_env(),
                schema=getattr(config, "storage_pg_schema", "teamevolver"),
                pool_min=1, pool_max=5, ssl=getattr(config, "storage_pg_ssl", "prefer"),
            )
        self.prefix = f"{self.runtime._schema}." if self.pg else ""
        self._initialization_key = (
            f"pg:{getattr(self.runtime, '_dsn', '')}:{self.prefix}"
            if self.pg else f"sqlite:{self.path.resolve()}"
        )

    def _run(self, operation):
        if self.pg:
            return self.runtime.run(operation, timeout=30)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(operation)
        # Compatibility for synchronous host helpers invoked inside async
        # routes. New route handlers use asyncio.to_thread.
        return _LOCAL_IO.submit(copy_context().run, asyncio.run, operation).result(timeout=30)

    @asynccontextmanager
    async def _connection(self, *, mutate=True):
        with _INITIALIZED_LOCK:
            needs_initialization = self._initialization_key not in _INITIALIZED
        if self.pg:
            async with self.runtime.admin_conn() as conn, conn.transaction():
                if mutate:
                    # Serialize last-admin checks across replicas.
                    lock = int.from_bytes(
                        hashlib.sha256(f"{self.prefix}:console-identity".encode()).digest()[:8],
                        "big", signed=True,
                    )
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1)",
                        lock,
                    )
                yield conn
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db_path = self.path
            fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(db_path, 0o600)
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE" if mutate or needs_initialization else "BEGIN")
                adapter = _SQLiteConnection(conn)
                if needs_initialization:
                    await self._initialize_sqlite(adapter)
                yield adapter
                conn.commit()
                if needs_initialization:
                    with _INITIALIZED_LOCK:
                        _INITIALIZED.add(self._initialization_key)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def _initialize_sqlite(self, conn):
        """Create only the isolated local test store, without importing files."""
        for table, columns in _TABLES.items():
            await conn.execute(f"CREATE TABLE IF NOT EXISTS {self.prefix}{table} ({columns})")

    async def _put(self, conn, table, uid, value):
        await conn.execute(
            f"INSERT INTO {self.prefix}{table}(id,record) VALUES($1,$2) "
            "ON CONFLICT(id) DO UPDATE SET record=excluded.record", uid, json.dumps(value),
        )

    async def _cleanup_expired_sessions(self, conn):
        now = time.time()
        login_rows = await conn.fetch(f"SELECT id,record FROM {self.prefix}console_login_sessions")
        expired_logins = {
            row["id"] for row in login_rows
            if json.loads(row["record"]).get("expires_at", 0) <= now
        }
        support_rows = await conn.fetch(f"SELECT id,record FROM {self.prefix}console_support_sessions")
        expired_support = {
            row["id"] for row in support_rows
            if json.loads(row["record"]).get("expires_at", 0) <= now
        }
        for sid in expired_logins:
            await conn.execute(f"DELETE FROM {self.prefix}console_login_sessions WHERE id=$1", sid)
            await conn.execute(f"DELETE FROM {self.prefix}console_support_sessions WHERE id=$1", sid)
        for sid in expired_support - expired_logins:
            await conn.execute(f"DELETE FROM {self.prefix}console_support_sessions WHERE id=$1", sid)

    async def _put_member(self, conn, member):
        await conn.execute(
            f"INSERT INTO {self.prefix}tenant_memberships(tenant_id,user_id,record) VALUES($1,$2,$3) "
            "ON CONFLICT(tenant_id,user_id) DO UPDATE SET record=excluded.record",
            member["tenant_id"], member["user_id"], json.dumps(member),
        )

    async def _audit(self, conn, actor, action, tenant_id, details):
        event = {"id": uuid.uuid4().hex, "created_at": time.time(), "actor": actor,
                 "action": action, "tenant_id": tenant_id, "details": details}
        await conn.execute(
            f"INSERT INTO {self.prefix}console_auth_audit(id,created_at,record) VALUES($1,$2,$3)",
            event["id"], event["created_at"], json.dumps(event),
        )
        return event

    def audit(self, actor, action, tenant_id="", **details):
        async def run():
            async with self._connection() as conn:
                return await self._audit(conn, actor, action, tenant_id, details)
        return self._run(run())

    def audit_events(self, limit=100):
        async def run():
            async with self._connection(mutate=False) as conn:
                rows = await conn.fetch(
                    f"SELECT record FROM {self.prefix}console_auth_audit ORDER BY created_at DESC LIMIT $1",
                    max(1, min(int(limit), 500)),
                )
                return [json.loads(row["record"]) for row in rows]
        return self._run(run())

    async def _users(self, conn):
        return [json.loads(row["record"]) for row in await conn.fetch(
            f"SELECT record FROM {self.prefix}console_users ORDER BY id"
        )]

    def load_registry(self):
        async def run():
            async with self._connection(mutate=False) as conn:
                return {"users": await self._users(conn)}
        return self._run(run())

    def session(self, token, *, value=None, delete=False, renew=False):
        sid = hashlib.sha256(token.encode()).hexdigest()
        async def run():
            async with self._connection(mutate=delete or value is not None) as conn:
                row = await conn.fetchrow(f"SELECT record FROM {self.prefix}console_login_sessions WHERE id=$1", sid)
                if delete:
                    await conn.execute(f"DELETE FROM {self.prefix}console_login_sessions WHERE id=$1", sid)
                    await conn.execute(f"DELETE FROM {self.prefix}console_support_sessions WHERE id=$1", sid)
                    return row is not None
                if value is not None:
                    if renew and row is None:
                        return None  # A peer logout must never be resurrected.
                    if not renew:
                        await self._cleanup_expired_sessions(conn)
                    await self._put(conn, "console_login_sessions", sid, value)
                    return value
                return json.loads(row["record"]) if row else None
        return self._run(run())

    def sessions(self):
        async def run():
            async with self._connection(mutate=False) as conn:
                return {row["id"]: json.loads(row["record"]) for row in await conn.fetch(
                    f"SELECT id,record FROM {self.prefix}console_login_sessions"
                )}
        return self._run(run())

    def edit_users(self, edit: Callable, *, actor="", action="user.update", membership_updates=None):
        """Run validation and read/modify/write in one transaction."""
        async def run():
            async with self._connection() as conn:
                before = await self._users(conn)
                data = {"users": copy.deepcopy(before)}
                result = edit(data)
                users = data["users"]
                if any(u.get("platform_role") == "platform_admin" and u.get("status") == "active" for u in before):
                    active_platform_admin = any(
                        u.get("platform_role") == "platform_admin" and u.get("status") == "active"
                        for u in users
                    )
                    if not active_platform_admin:
                        raise HTTPException(409, "LAST_PLATFORM_ADMIN")
                next_ids = {u["id"] for u in users}
                disabled_ids = {u["id"] for u in before} - {
                    u["id"] for u in users if u.get("status", "active") == "active"
                }
                memberships = [json.loads(row["record"]) for row in await conn.fetch(
                    f"SELECT record FROM {self.prefix}tenant_memberships"
                )]
                active_ids = {u["id"] for u in users if u.get("status", "active") == "active"}
                for member in memberships:
                    if member["user_id"] in disabled_ids and member["role"] == "tenant_admin" \
                            and member["status"] == "active":
                        if not any(m["tenant_id"] == member["tenant_id"] and m["role"] == "tenant_admin"
                                   and m["status"] == "active" and m["user_id"] in active_ids
                                   for m in memberships):
                            raise HTTPException(409, "LAST_TENANT_ADMIN")
                for old in before:
                    if old["id"] not in next_ids:
                        await conn.execute(f"DELETE FROM {self.prefix}console_users WHERE id=$1", old["id"])
                        await conn.execute(f"DELETE FROM {self.prefix}tenant_memberships WHERE user_id=$1", old["id"])
                for user in users:
                    user.pop("role", None)
                    user.setdefault("platform_role", "user")
                    user.setdefault("status", "active")
                    await self._put(conn, "console_users", user["id"], user)
                for member in membership_updates or []:
                    await self._put_member(conn, member)
                    await self._audit(conn, actor, "membership.import", member["tenant_id"], {
                        "user_id": member["user_id"], "role": member["role"],
                    })
                old = {u["id"]: u for u in before}
                changes = [{key: u.get(key) for key in ("id", "platform_role", "status")}
                           for u in users if u != old.get(u["id"])]
                await self._audit(conn, actor, action, "", {
                    "changes": changes, "removed": sorted(set(old) - next_ids),
                })
                return result
        return self._run(run())

    def memberships(self, user_id=None, tenant_id=None):
        async def run():
            async with self._connection(mutate=False) as conn:
                clauses, args = [], []
                for key, value in (("user_id", user_id), ("tenant_id", tenant_id)):
                    if value is not None:
                        args.append(value)
                        clauses.append(f"{key}=${len(args)}")
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = await conn.fetch(f"SELECT record FROM {self.prefix}tenant_memberships{where}", *args)
                return [json.loads(row["record"]) for row in rows]
        return self._run(run())

    def set_member(self, tenant_id, user_id, role, status, actor):
        if role not in {"tenant_admin", "member"} or status not in {"active", "disabled"}:
            raise HTTPException(400, "INVALID_MEMBERSHIP")
        async def run():
            async with self._connection() as conn:
                users = await self._users(conn)
                if not any(u["id"] == user_id for u in users):
                    raise HTTPException(404, "USER_NOT_FOUND")
                active_ids = {u["id"] for u in users if u.get("status", "active") == "active"}
                if status == "active" and user_id not in active_ids:
                    raise HTTPException(409, "USER_DISABLED")
                rows = await conn.fetch(
                    f"SELECT record FROM {self.prefix}tenant_memberships WHERE tenant_id=$1", tenant_id
                )
                members = [json.loads(row["record"]) for row in rows]
                existing = next((m for m in members if m["user_id"] == user_id), {})
                admins = [m for m in members if m["role"] == "tenant_admin" and m["status"] == "active"
                          and m["user_id"] in active_ids]
                if (existing in admins and len(admins) == 1 and (role != "tenant_admin" or status != "active")):
                    raise HTTPException(409, "LAST_TENANT_ADMIN")
                member = {**existing, "tenant_id": tenant_id, "user_id": user_id, "role": role, "status": status,
                          "created_at": existing.get("created_at", time.time()), "updated_at": time.time(),
                          "created_by": existing.get("created_by", actor)}
                await self._put_member(conn, member)
                await self._audit(conn, actor, "membership.update", tenant_id, {
                    "user_id": user_id, "role": role, "status": status,
                })
                return member
        return self._run(run())

    def support(self, actor, session_token, tenant_id, *, reason=None, stop=False):
        sid = hashlib.sha256(session_token.encode()).hexdigest()
        async def run():
            async with self._connection(mutate=stop or reason is not None) as conn:
                row = await conn.fetchrow(
                    f"SELECT record FROM {self.prefix}console_support_sessions WHERE id=$1", sid
                )
                record = json.loads(row["record"]) if row else {}
                if stop:
                    await conn.execute(f"DELETE FROM {self.prefix}console_support_sessions WHERE id=$1", sid)
                    await self._audit(conn, actor, "support.stop", record.get("tenant_id", ""), {})
                    return None
                if reason is not None:
                    if not session_token or not str(reason).strip():
                        raise HTTPException(400, "SUPPORT_REASON_REQUIRED")
                    session = await conn.fetchrow(
                        f"SELECT record FROM {self.prefix}console_login_sessions WHERE id=$1", sid
                    )
                    login = json.loads(session["record"]) if session else {}
                    if login.get("user_id") != actor or login.get("expires_at", 0) <= time.time():
                        raise HTTPException(401, "login required")
                    record = {"actor": actor, "tenant_id": tenant_id, "reason": str(reason).strip()[:500],
                              "expires_at": time.time() + 1800, "id": uuid.uuid4().hex}
                    await self._put(conn, "console_support_sessions", sid, record)
                    await self._audit(conn, actor, "support.start", tenant_id, record)
                if (record.get("actor") == actor and record.get("tenant_id") == tenant_id
                        and record.get("expires_at", 0) > time.time()):
                    return record
                return None
        return self._run(run())

    def support_authorized(self, actor, tenant_id, support_id):
        async def run():
            async with self._connection(mutate=False) as conn:
                rows = await conn.fetch(f"SELECT record FROM {self.prefix}console_support_sessions")
                return any(r.get("actor") == actor and r.get("tenant_id") == tenant_id
                           and r.get("id") == support_id and r.get("expires_at", 0) > time.time()
                           for r in (json.loads(row["record"]) for row in rows))
        return self._run(run())
