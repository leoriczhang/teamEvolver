"""Lifecycle and tenant-authenticated status for the successful experience mirror."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request

from team_skills.library.experience_sync import ROOT, ExperienceSync, OVWriter, Settings, SyncError, Target
from teamEvolver.logging_runtime import event
from teamEvolver.storage.pg_store import PgObjectStore
from teamEvolver.tenants.registry import current_tenant_id, effective_config

LOG = logging.getLogger(__name__)


def build_store(config, tenant):
    return PgObjectStore(
        dsn=config.storage_pg_dsn, schema=config.storage_pg_schema, tenant_id=tenant,
        # Two concurrent passes each hold a lock connection; leave room for
        # data/status queries even on installations with a tiny default pool.
        pool_min=config.storage_pg_pool_min, pool_max=max(4, config.storage_pg_pool_max),
        command_timeout=config.storage_pg_command_timeout_seconds, ssl=config.storage_pg_ssl,
    )


class SyncRuntime:
    def __init__(self, owner, registry_getter):
        self.owner, self.registry_getter = owner, registry_getter
        self.stop_event = threading.Event()
        self.executor = None
        self.task = None
        self.failures = {}
        self.activity = {}
        self.requested = set()
        self.registering = set()
        self.wake = asyncio.Event()
        self.trigger_slots = asyncio.Semaphore(2)

    def register_run(self, ctx, operation="sync"):
        config = effective_config(None, ctx, self.owner.config)
        settings = Settings.from_config(config)
        if not settings.enabled:
            raise SyncError("SYNC_DISABLED", 409)
        target = Target.from_config(config)
        worker = ExperienceSync(build_store(config, ctx.tenant_id), target, settings, None, stop=self.stop_event)
        return worker.request_run(operation)

    async def trigger(self, ctx, operation="sync"):
        if not Settings.from_config(effective_config(None, ctx, self.owner.config)).enabled:
            raise SyncError("SYNC_DISABLED", 409)
        if self.task is None or self.task.done() or self.stop_event.is_set():
            raise SyncError("SYNC_NOT_RUNNING", 503)
        admitted = self.requested | self.registering
        if (self.trigger_slots.locked() or ctx.tenant_id in self.registering
                or (len(admitted) >= 32 and ctx.tenant_id not in admitted)):
            raise SyncError("SYNC_BUSY", 429)
        async with self.trigger_slots:
            self.registering.add(ctx.tenant_id)
            registration = asyncio.create_task(asyncio.to_thread(self.register_run, ctx, operation))
            try:
                receipt = await asyncio.shield(registration)
            except asyncio.CancelledError:
                # Keep the bounded slot until the DB thread exits. A disconnected
                # caller must not create unbounded, still-running registrations.
                await registration
                raise
            finally:
                self.registering.discard(ctx.tenant_id)
                if registration.done() and not registration.cancelled() and registration.exception() is None:
                    self.requested.add(ctx.tenant_id)
                self.wake.set()
        event(LOG, "experience_sync.manual_accepted", tenant=ctx.tenant_id,
              sync_request_id=receipt["request_id"], state=receipt["state"])
        return {"tenant_id": ctx.tenant_id, "manual": receipt}

    def failure(self, tenant, code, **diagnostics):
        # First occurrence/change is immediate; unchanged failures are summarized
        # once per minute without retaining arbitrary exception strings.
        previous, at = self.failures.get(tenant, (None, 0))
        if previous != code or time.monotonic() - at >= 60:
            event(LOG, "experience_sync.blocked", logging.WARNING, tenant=tenant, code=code, **diagnostics)
            self.failures[tenant] = (code, time.monotonic())

    def run_tenant(self, ctx):
        writer = None
        stage = "configuration"
        self.activity[ctx.tenant_id] = {"state": "running", "started_at": time.time(), "pid": os.getpid()}
        try:
            config = effective_config(None, ctx, self.owner.config)
            settings = Settings.from_config(config)
            if not settings.enabled or ctx.status != "active" or self.stop_event.is_set():
                return 30
            target = Target.from_config(config)
            stage = "storage_initialization"
            store = build_store(config, ctx.tenant_id)
            writer = OVWriter(target)

            def guard():
                if self.stop_event.is_set():
                    raise SyncError("SYNC_STOPPING")
                store.check_background_lock("successful-experience-sync-v1")

            writer.guard = guard
            worker = ExperienceSync(store, target, settings, writer, stop=self.stop_event)
            stage = "worker_pass"
            more = worker.run_once()
            self.activity[ctx.tenant_id] = {**self.activity[ctx.tenant_id], "state": worker.outcome}
            if self.failures.pop(ctx.tenant_id, None):
                event(LOG, "experience_sync.recovered", tenant=ctx.tenant_id)
            return 1 if more else settings.interval_seconds
        except Exception as exc:
            self.failure(ctx.tenant_id, exc.code if isinstance(exc, SyncError) else "SYNC_STORAGE_FAILURE",
                         stage=stage, error_type=type(exc).__name__)
            return 30
        finally:
            self.activity[ctx.tenant_id] = {**self.activity[ctx.tenant_id],
                "state": "blocked" if ctx.tenant_id in self.failures else (
                    "lock_busy" if self.activity[ctx.tenant_id]["state"] == "lock_busy" else "idle"),
                "finished_at": time.time(),
                "error": self.failures.get(ctx.tenant_id, (None, 0))[0]}
            if writer is not None:
                writer.close()

    async def run(self):
        pending, due = {}, {}
        loop = asyncio.get_running_loop()
        try:
            while not self.stop_event.is_set():
                for tenant, future in list(pending.items()):
                    if future.done():
                        try:
                            delay = future.result()
                        except Exception:
                            self.failure(tenant, "SYNC_WORKER_FAILURE")
                            delay = 30
                        due[tenant] = time.monotonic() + delay
                        del pending[tenant]
                try:
                    registry = self.registry_getter(self.owner)
                    contexts = await asyncio.to_thread(registry.list_tenants)
                    self.requested.intersection_update(ctx.tenant_id for ctx in contexts)
                    for ctx in sorted(contexts, key=lambda ctx: (
                        ctx.tenant_id not in self.requested, due.get(ctx.tenant_id, 0),
                    )):
                        if len(pending) >= 2 or self.stop_event.is_set():
                            break
                        try:
                            config = effective_config(registry, ctx, self.owner.config)
                            enabled = Settings.from_config(config).enabled
                        except Exception:
                            self.failure(ctx.tenant_id, "INVALID_SYNC_CONFIG")
                            self.requested.discard(ctx.tenant_id)
                            continue
                        if ctx.status != "active" or not enabled:
                            self.requested.discard(ctx.tenant_id)
                            continue
                        if ctx.tenant_id not in pending and (
                            ctx.tenant_id in self.requested or time.monotonic() >= due.get(ctx.tenant_id, 0)
                        ):
                            self.requested.discard(ctx.tenant_id)
                            pending[ctx.tenant_id] = loop.run_in_executor(self.executor, self.run_tenant, ctx)
                except Exception as exc:
                    self.failure("registry", exc.code if isinstance(exc, SyncError) else "SYNC_REGISTRY_FAILURE")
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
        finally:
            # HTTP has a 30s timeout and checks stop before every next operation.
            # Do not cancel futures: that would release lifecycle ownership while
            # a thread could still be writing to OV.
            if pending:
                await asyncio.gather(*pending.values(), return_exceptions=True)

    def start(self):
        if not self.owner.config.storage_pg_enabled:
            try:
                if Settings.from_config(self.owner.config).enabled:
                    self.failure("default", "PG_REQUIRED")
            except SyncError as exc:
                self.failure("default", exc.code)
            return
        if self.task is None:
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="experience-sync")
            self.task = asyncio.create_task(self.run())
            event(LOG, "experience_sync.started")

    async def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.task:
            await self.task
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
        event(LOG, "experience_sync.stopped")

    def status(self, ctx):
        config = effective_config(None, ctx, self.owner.config)
        tenant = ctx.tenant_id
        result = {"enabled": False, "tenant_id": tenant, "target_directory": ROOT,
                  "counts": {"pending": 0, "synced": 0, "retry": 0}, "last_scan": None, "last_error": None,
                  "manual": None}
        try:
            settings = Settings.from_config(config)
            result["enabled"] = settings.enabled
            result["target_directory"] = settings.target_directory
            if not settings.enabled:
                return result
            target = Target.from_config(config)
            worker = ExperienceSync(build_store(config, tenant), target, settings, None)
            result.update(worker.status())
            result["last_error"] = result["last_error"] or self.failures.get(tenant, (None, 0))[0]
        except Exception as exc:
            result["last_error"] = exc.code if isinstance(exc, SyncError) else "SYNC_STORAGE_FAILURE"
        running = self.task is not None and not self.task.done() and not self.stop_event.is_set()
        result["worker"] = {**self.activity.get(tenant, {"state": "waiting", "pid": os.getpid()}),
                            "scheduler_running": running, "registry_error": self.failures.get("registry", (None, 0))[0]}
        if not running:
            result["worker"]["state"] = "stopped"
        return result


def install(app, owner, registry_getter, admin_guard):
    runtime = SyncRuntime(owner, registry_getter)
    app.state.experience_sync = runtime
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            runtime.start()
            try:
                yield
            finally:
                await runtime.stop()

    app.router.lifespan_context = lifespan

    async def context(request):
        admin_guard(getattr(request.state, "console_user", None))
        registry = registry_getter(owner)
        ctx = await asyncio.to_thread(registry.get, current_tenant_id())
        if ctx is None or ctx.status != "active":
            raise HTTPException(403, "tenant is not active")
        return ctx

    @app.get("/api/experience-sync/status", tags=["operations"])
    async def status(request: Request):
        ctx = await context(request)
        return await asyncio.to_thread(runtime.status, ctx)

    @app.post("/api/experience-sync/trigger", tags=["operations"], status_code=202)
    async def trigger(request: Request):
        ctx = await context(request)
        try:
            return await runtime.trigger(ctx)
        except SyncError as exc:
            raise HTTPException(exc.status or 503, exc.code) from None
        except Exception:
            runtime.failure(ctx.tenant_id, "SYNC_STORAGE_FAILURE")
            raise HTTPException(503, "SYNC_STORAGE_FAILURE") from None


    @app.post("/api/experience-sync/import", tags=["operations"], status_code=202)
    async def import_history(request: Request):
        ctx = await context(request)
        try:
            return await runtime.trigger(ctx, "import_all")
        except SyncError as exc:
            raise HTTPException(exc.status or 503, exc.code) from None
        except Exception:
            runtime.failure(ctx.tenant_id, "SYNC_STORAGE_FAILURE")
            raise HTTPException(503, "SYNC_STORAGE_FAILURE") from None
