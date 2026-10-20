"""Tenant-scoped, revision-checked metadata for the preserved mining algorithms.

The synchronous protocol runs on DatabasePool's dedicated IO loop. HTTP and
Worker callers invoke the core repository through asyncio.to_thread; no local
JSON file is read as authoritative metadata.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from typing import Any


class MiningRevisionConflict(RuntimeError):
    """Another replica changed the metadata; reload before applying changes."""

    status_code = 409


class MiningCapacityExceeded(ValueError):
    """The tenant already occupies all bounded active-job slots."""

    status_code = 429


def identity(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", text) or text in {".", ".."}:
        raise ValueError("invalid mining identity")
    return text


def decode(value):
    return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PgMiningBackend:
    """Required core backend; each write has an optimistic revision guard."""

    def __init__(self, tx, tenant_id, *, services=None, task=None, max_active=8):
        self.tx, self.tenant_id = tx, str(tenant_id)
        self.services, self.task = services, task
        self.schema = tx.pool.schema
        self.max_active = max(1, int(max_active))
        self._lock = threading.RLock()
        self._job_revisions: dict[str, int] = {}
        self._file_revisions: dict[str, int] = {}
        self._source_snapshot: dict[str, tuple[int, dict]] | None = None
        self.before_save = None
        self.transform_save = None
        self.cache_project_root = None

    def _run(self, body, *, timeout=60):
        return self.tx.run_tenant_tx(self.tenant_id, body, timeout=timeout)

    async def guard(self, conn):
        if self.task is not None and self.services is not None:
            await self.services.tasks.require_current(conn, self.task, allow_cancel=True)
            heartbeat = getattr(self.services.tasks, "heartbeat", None)
            if callable(heartbeat):
                # A streaming snapshot holds this Worker row until commit;
                # renew through that same transaction rather than starving the
                # independent heartbeat behind our own row lock.
                renewed = await heartbeat(conn, task_id=self.task.task_id, owner_id=self.task.owner_id,
                                          fence=self.task.fence, lease_seconds=60)
                if not renewed:
                    raise RuntimeError("worker attempt lost its lease")

    def _remember(self, row):
        name = str(row["job_id"])
        self._job_revisions[name] = int(row["revision"])
        self._file_revisions.setdefault(name, int(row["files_revision"]))
        value = decode(row["document"])
        if self.cache_project_root is not None:
            from .cache_locators import locators
            value = locators(value, self.cache_project_root / "mining_jobs" / name, name, portable=False)
        return value

    def load_job(self, job_id):
        name = identity(job_id)

        async def read(conn):
            return await conn.fetchrow(
                f"SELECT job_id,revision,files_revision,document FROM {self.schema}.skill_mining_jobs "
                "WHERE tenant_id=$1 AND job_id=$2", self.tenant_id, name,
            )

        with self._lock:
            row = self._run(read)
            if row is None:
                self._job_revisions[name] = 0
                self._file_revisions[name] = 0
                return None
            return self._remember(row)

    def list_jobs(self):
        async def read(conn):
            return await conn.fetch(
                f"SELECT job_id,revision,files_revision,document FROM {self.schema}.skill_mining_jobs "
                "WHERE tenant_id=$1 ORDER BY created_at DESC,job_id", self.tenant_id,
            )

        with self._lock:
            return [self._remember(row) for row in self._run(read)]

    def save_job(self, job):
        value = copy.deepcopy(job)
        if self.transform_save is not None:
            value = self.transform_save(value)
        name = identity(value.get("job_id"))
        status, deleted = str(value.get("status") or "queued"), bool(value.get("_deleted"))
        stored = value
        if self.cache_project_root is not None:
            from .cache_locators import locators
            stored = locators(value, self.cache_project_root / "mining_jobs" / name, name, portable=True)
        with self._lock:
            expected = self._job_revisions.get(name, 0)
            if expected and self.before_save is not None:
                self.before_save(copy.deepcopy(value))

            async def write(conn):
                await self.guard(conn)
                if status in {"preparing", "queued", "running", "stopping"} and not deleted:
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                       self.tenant_id + ":skill-mining-active-jobs")
                    current = await conn.fetchval(
                        f"SELECT status FROM {self.schema}.skill_mining_jobs WHERE tenant_id=$1 "
                        "AND job_id=$2 AND NOT deleted", self.tenant_id, name,
                    )
                    if current not in {"preparing", "queued", "running", "stopping"}:
                        count = await conn.fetchval(
                            f"SELECT count(*) FROM {self.schema}.skill_mining_jobs WHERE tenant_id=$1 AND NOT deleted "
                            "AND status IN ('preparing','queued','running','stopping')", self.tenant_id,
                        )
                        if count >= self.max_active:
                            raise MiningCapacityExceeded(f"最多同时运行 {self.max_active} 个 Skill 挖掘任务")
                if expected == 0:
                    revision = await conn.fetchval(
                        f"INSERT INTO {self.schema}.skill_mining_jobs "
                        "(tenant_id,job_id,status,deleted,document) VALUES ($1,$2,$3,$4,$5::jsonb) "
                        "ON CONFLICT (tenant_id,job_id) DO NOTHING RETURNING revision",
                        self.tenant_id, name, status, deleted, encode(stored),
                    )
                else:
                    revision = await conn.fetchval(
                        f"UPDATE {self.schema}.skill_mining_jobs SET status=$3,deleted=$4,document=$5::jsonb,"
                        "revision=revision+1,updated_at=now() WHERE tenant_id=$1 AND job_id=$2 "
                        "AND revision=$6 RETURNING revision",
                        self.tenant_id, name, status, deleted, encode(stored), expected,
                    )
                if revision is None:
                    raise MiningRevisionConflict(f"Skill mining job {name} changed; reload before saving")
                return int(revision)

            self._job_revisions[name] = self._run(write)
            self._file_revisions.setdefault(name, 0)
        return copy.deepcopy(value)

    def load_sources(self):
        async def read(conn):
            return await conn.fetch(
                f"SELECT source_id,revision,document FROM {self.schema}.skill_mining_sources "
                "WHERE tenant_id=$1 ORDER BY created_at,source_id", self.tenant_id,
            )

        with self._lock:
            self._source_snapshot = {
                str(row["source_id"]): (int(row["revision"]), decode(row["document"])) for row in self._run(read)
            }
            return [copy.deepcopy(value) for _, value in self._source_snapshot.values()]

    def save_sources(self, rows):
        values = {identity(row.get("source_id")): copy.deepcopy(row) for row in rows}
        if len(values) != len(rows):
            raise ValueError("duplicate mining source identity")
        with self._lock:
            if self._source_snapshot is None:
                raise MiningRevisionConflict("load sources before saving their changes")
            before = self._source_snapshot

            async def write(conn):
                await self.guard(conn)
                revisions = {key: revision for key, (revision, _) in before.items() if key in values}
                for key in sorted(set(before) - set(values)):
                    removed = await conn.fetchval(
                        f"DELETE FROM {self.schema}.skill_mining_sources WHERE tenant_id=$1 AND source_id=$2 "
                        "AND revision=$3 RETURNING revision", self.tenant_id, key, before[key][0],
                    )
                    if removed is None:
                        raise MiningRevisionConflict("mining source changed before deletion")
                for key, value in values.items():
                    prior = before.get(key)
                    if prior is not None and prior[1] == value:
                        continue
                    account, uri = str(value.get("account") or ""), str(value.get("uri") or "")
                    if not uri.startswith("viking://resources/"):
                        raise ValueError("mining source must be an OpenViking Resource URI")
                    if prior is None:
                        revision = await conn.fetchval(
                            f"INSERT INTO {self.schema}.skill_mining_sources "
                            "(tenant_id,source_id,account,uri,document) VALUES ($1,$2,$3,$4,$5::jsonb) "
                            "ON CONFLICT (tenant_id,source_id) DO NOTHING RETURNING revision",
                            self.tenant_id, key, account, uri, encode(value),
                        )
                    else:
                        revision = await conn.fetchval(
                            f"UPDATE {self.schema}.skill_mining_sources SET account=$3,uri=$4,document=$5::jsonb,"
                            "revision=revision+1,updated_at=now() WHERE tenant_id=$1 AND source_id=$2 "
                            "AND revision=$6 RETURNING revision",
                            self.tenant_id, key, account, uri, encode(value), prior[0],
                        )
                    if revision is None:
                        raise MiningRevisionConflict("mining source changed; reload before saving")
                    revisions[key] = int(revision)
                return revisions

            revisions = self._run(write)
            self._source_snapshot = {key: (revisions[key], value) for key, value in values.items()}


def make_repository(services, tenant_id, task=None):
    """Construct the preserved core over PG metadata and a disposable cache."""
    from .cache import cache_root
    from .core_repository import OvSkillMiningRepository

    root = cache_root(services, tenant_id)
    backend = PgMiningBackend(services.tx, tenant_id, services=services, task=task)
    backend.cache_project_root = root
    return OvSkillMiningRepository(root / "state", root, backend=backend)
