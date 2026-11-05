"""Run the preserved mining core inside the platform Worker and PG fences."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextvars import ContextVar

from starlette.exceptions import HTTPException

from .cache import job_root, persist_cache, restore_cache
from .engine_admission import CompileAdmission, MiningAdmissionMixin
from .engine_polling import MiningPollingMixin
from .engine_resources import MiningResourceMixin
from .native_compiler import freeze_assets
from .ov_compile_mining import ACTIVE_JOB_STATES, utc_now
from .ov_round_orchestration import OvRoundOrchestrationMixin
from .platform_client import CompileClientCompat, create_client, request, settings_parts
from .repository import make_repository
from .settings import compiler_prompt


class SkillMiningEngine(MiningAdmissionMixin, MiningPollingMixin, MiningResourceMixin, OvRoundOrchestrationMixin):
    def __init__(self, services, tenant, settings, task=None):
        self.services, self.tenant, self.settings, self.task = services, str(tenant), settings, task
        self.miner_config, self.configuration = settings_parts(settings)
        self.repository = None
        self.client = None
        self.admission = CompileAdmission(services, self.tenant, task)
        self.pending_submission = False
        self._loaded_job = ""
        self._active_job = ""
        self._cache_signatures = {}
        self._lease_lost = False
        self._lease_owner = "mining-engine:" + uuid.uuid4().hex
        self._held_lease = ContextVar("skill_mining_lease_" + uuid.uuid4().hex, default="")
        self._lease_fence = None
        self._operation_lock = asyncio.Lock()

    async def prepare(self):
        if self.repository is None:
            self.repository = await asyncio.to_thread(make_repository, self.services, self.tenant, self.task)
        if self.client is None:
            self.client = await create_client(self.services, self.settings)
        return self

    async def close(self):
        if self.repository is not None:
            self.repository.backend.before_save = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def _ov_skill_mining_repository(self):
        if self.repository is None:
            raise RuntimeError("prepare the mining engine before using its repository")
        return self.repository

    def _ov_skill_mining_lock(self, job_id):
        return self.job_lock(job_id)

    def _round_client(self, job):
        self._check_account(job.get("account"))
        return CompileClientCompat(client=self.client, guard=self._guard)

    def _knowledge_endpoint(self):
        return ""

    def _knowledge_api_key(self):
        return ""

    def _knowledge_team_user(self):
        return str(self.miner_config.get("user_id") or "")

    def _ov_skill_compiler_body(self):
        return compiler_prompt(self.configuration)

    def _check_account(self, account):
        expected = str(self.miner_config.get("account_id") or "")
        if not expected or str(account or "") != expected:
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")

    async def _guard(self):
        if self._lease_lost:
            raise RuntimeError("Skill mining resource lease was lost")
        if self.task is not None or self._lease_fence is not None:
            async with self.services.tx.tenant_tx(self.tenant) as conn:
                await self._guard_connection(conn)

    async def _guard_connection(self, conn):
        if self.task is not None:
            await self.services.tasks.require_current(conn, self.task, allow_cancel=True)
        if self._lease_fence is not None:
            current = await self.services.leases.heartbeat(
                conn, tenant_id=self.tenant, resource_key="skill-mining:job:" + self._lease_job,
                owner_id=self._lease_owner, fence=self._lease_fence, lease_seconds=60)
            if not current:
                self._lease_lost = True
                raise RuntimeError("Skill mining resource lease was lost")

    async def _knowledge_request(self, account, method, path, *, timeout=None, params=None, json=None,
                                 extra_headers=None):
        self._check_account(account)
        await self._guard()
        key = str((extra_headers or {}).get("Idempotency-Key") or "")
        if method.upper() == "POST" and path == "/api/v1/compile" and self._active_job:
            await asyncio.to_thread(self._checkpoint, {"job_id": self._active_job})
        async with asyncio.timeout(float(timeout)) if timeout else contextlib.nullcontext():
            return await request(self.client, method, path, body=json, params=params, key=key)

    def _checkpoint(self, job):
        if self._lease_lost:
            raise RuntimeError("Skill mining resource lease was lost before artifact checkpoint")
        if self.task is not None or self._lease_fence is not None:
            self.services.tx.run_tenant_tx(self.tenant, self._guard_connection, timeout=30)
        name = job["job_id"]
        root = job_root(self.repository, name)
        signature = tuple(sorted(
            (path.relative_to(root).as_posix(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()
        ))
        if self._cache_signatures.get(name) != signature:
            persist_cache(self.repository, name)
            self._cache_signatures[name] = signature

    async def _restore(self, job):
        name = job["job_id"]
        if self._loaded_job != name:
            self.repository.backend.before_save = None
            await asyncio.to_thread(restore_cache, self.repository, name)
            root = job_root(self.repository, name)
            compiler = root / "compiler-skill.md"
            if not compiler.is_file() and job.get("compiler_body_snapshot"):
                # A crash between the initial job insert and first file checkpoint
                # can rebuild this exact frozen compiler from PG metadata.
                def restore_compiler():
                    root.mkdir(parents=True, exist_ok=True)
                    compiler.write_text(job["compiler_body_snapshot"], encoding="utf-8")
                    freeze_assets(root, job["compiler_body_snapshot"])
                await asyncio.to_thread(restore_compiler)
            # File snapshots omit empty directories. The core still needs its
            # initial workspace when the first remote output is materialized.
            def restore_directories():
                for relative in ("workspace", "workspace/compiled_skill", "workspace/semantic_reports"):
                    path = root / relative
                    if not path.exists():
                        path.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(restore_directories)
            self._loaded_job = name
            self.repository.backend.before_save = self._checkpoint

    @contextlib.asynccontextmanager
    async def job_lock(self, name):
        """Reentrant per-task PG lease covering cache restoration and all mutations."""
        held = self._held_lease.get()
        if held and held[0] is asyncio.current_task():
            if held[1] != name:
                raise RuntimeError("Cannot nest mining locks for different jobs")
            await self._guard()
            yield
            return
        async with self._operation_lock:
            async with self._job_lease(name):
                yield

    @contextlib.asynccontextmanager
    async def _job_lease(self, name):
        key = "skill-mining:job:" + name
        self._lease_lost = False
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            fence = await self.services.leases.acquire(
                conn, tenant_id=self.tenant, resource_key=key, owner_id=self._lease_owner, lease_seconds=60)
        if fence is None:
            raise HTTPException(409, "Skill mining job is owned by another Worker")
        # An operation on another Worker may have edited the PG artifact snapshot
        # while this engine was between polls. Never reuse a former owner's cache.
        self._loaded_job = ""
        self._cache_signatures.pop(name, None)
        if self.repository is not None:
            self.repository.backend.before_save = None
        self._lease_job, self._lease_fence = name, fence
        token = self._held_lease.set((asyncio.current_task(), name))

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(20)
                    async with self.services.tx.tenant_tx(self.tenant) as conn:
                        current = await self.services.leases.heartbeat(
                            conn, tenant_id=self.tenant, resource_key=key, owner_id=self._lease_owner,
                            fence=fence, lease_seconds=60)
                    if not current:
                        self._lease_lost = True
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                self._lease_lost = True

        keeper = asyncio.create_task(heartbeat())
        try:
            yield
            if self._lease_lost:
                raise RuntimeError("Skill mining resource lease was lost")
        finally:
            self._held_lease.reset(token)
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
            try:
                async with self.services.tx.tenant_tx(self.tenant) as conn:
                    await self.services.leases.release(
                        conn, tenant_id=self.tenant, resource_key=key, owner_id=self._lease_owner, fence=fence)
            finally:
                self._lease_fence = None
                if self.repository is not None:
                    self.repository.backend.before_save = None

    async def restore(self, job_id):
        """Restore PG artifacts; callers hold job_lock across subsequent operations."""
        await self.prepare()
        async with self.job_lock(job_id):
            job = await asyncio.to_thread(self.repository.get_job, job_id)
            self._check_account(job.get("account"))
            await self._restore(job)
            return job

    async def _operate(self, job, action, *, detail=False, reason=""):
        await self.prepare()
        name = str(job.get("job_id") if isinstance(job, dict) else job)
        async with self.job_lock(name):
            current = await asyncio.to_thread(self.repository.get_job, name)
            self._check_account(current.get("account"))
            await self._restore(current)
            self._active_job = name
            self.pending_submission = False
            try:
                if action == "submit":
                    await self._submit_ov_skill_round(current)
                elif action == "poll":
                    if current.get("remote_terminal_unconfirmed") and current.get("status") == "interrupted":
                        await self._reconcile_unconfirmed_ov_skill_job(current)
                    else:
                        await self._refresh_ov_skill_job(current, detail=detail)
                else:
                    await self._stop(current, reason or "用户手动停止")
                await asyncio.to_thread(self._checkpoint, current)
                return await asyncio.to_thread(self.repository.get_job, name)
            finally:
                self._active_job = ""

    async def submit(self, job):
        return await self._operate(job, "submit")

    async def poll(self, job, detail=False):
        return await self._operate(job, "poll", detail=detail)

    async def stop(self, job, reason=""):
        return await self._operate(job, "stop", reason=reason)

    async def _stop(self, job, reason):
        name = job["job_id"]
        async with self._ov_skill_mining_lock(name):
            job = await asyncio.to_thread(self.repository.get_job, name)
            if job.get("status") == "waiting_human":
                return await asyncio.to_thread(self.repository.stop_waiting, name)
            if job.get("status") == "preparing" and not job.get("ov_task_id"):
                if not job.get("compile_request"):
                    return await asyncio.to_thread(self.repository.update_job, name, status="stopped",
                                                   finished_at=utc_now(), stop_reason=reason)
                job = await asyncio.to_thread(self.repository.update_job, name, status="stopping",
                    stop_reason=reason + "；正在确认远端受理结果", ov_cancel_reason=reason,
                    ov_cancel_requested_at=utc_now())
                self.pending_submission = True
                return job
            if job.get("status") not in ACTIVE_JOB_STATES:
                raise HTTPException(409, "该挖掘任务当前不可停止")
            job = await asyncio.to_thread(self.repository.update_job, name, status="stopping",
                stop_reason=job.get("stop_reason") or reason, ov_cancel_reason=job.get("ov_cancel_reason") or reason,
                ov_cancel_requested_at=job.get("ov_cancel_requested_at") or utc_now())
        return await self._refresh_ov_skill_job(job)

    def _schedule_ov_skill_round_submission(self, job):
        self.pending_submission = True

    async def _freeze_round_inputs(self, job, *, staged=False):
        result = await super()._freeze_round_inputs(job, staged=staged)
        await asyncio.to_thread(self._checkpoint, job)
        return result

    async def _native_inputs(self, job):
        result = await super()._native_inputs(job)
        await asyncio.to_thread(self._checkpoint, job)
        return result
