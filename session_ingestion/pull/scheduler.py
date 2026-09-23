"""Daily, tenant-scoped scheduling for pull-based Session ingestion."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from session_ingestion.adapters import _runtime as adapters
from session_ingestion.service import ingest
from teamEvolver.tenants.registry import (
    reset_current_tenant,
    set_current_tenant,
)

from .service import pull_sessions

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE = {
    "enabled": False,
    "time": "00:00",
    "timezone": "Asia/Shanghai",
    "window": "previous_day",
    "max_sessions": 1000,
}
_SCHEDULE_FIELDS = frozenset(DEFAULT_SCHEDULE)


class PullBusyError(RuntimeError):
    """Raised when the tenant already has a datasource pull in progress."""


class PullCapacityError(RuntimeError):
    """Raised when the process-wide datasource pull capacity is exhausted."""


def normalize_schedule(value: Any) -> dict[str, Any]:
    """Validate and normalize the persisted daily pull configuration."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("datasource schedule must be an object")
    unknown = set(value) - _SCHEDULE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported schedule fields: {', '.join(sorted(unknown))}")

    enabled = value.get("enabled", DEFAULT_SCHEDULE["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("schedule.enabled must be boolean")

    trigger_time = str(value.get("time", DEFAULT_SCHEDULE["time"]) or "").strip()
    try:
        hour_text, minute_text = trigger_time.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        raise ValueError("schedule.time must use HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59) or trigger_time != f"{hour:02d}:{minute:02d}":
        raise ValueError("schedule.time must use 24-hour HH:MM")

    timezone_name = str(
        value.get("timezone", DEFAULT_SCHEDULE["timezone"]) or ""
    ).strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown schedule timezone: {timezone_name}") from None

    window = str(value.get("window", DEFAULT_SCHEDULE["window"]) or "").strip()
    if window != "previous_day":
        raise ValueError("schedule.window must be previous_day")

    max_sessions = value.get("max_sessions", DEFAULT_SCHEDULE["max_sessions"])
    if isinstance(max_sessions, bool) or not isinstance(max_sessions, int):
        raise ValueError("schedule.max_sessions must be an integer")
    if not 1 <= max_sessions <= 1000:
        raise ValueError("schedule.max_sessions must be between 1 and 1000")

    return {
        "enabled": enabled,
        "time": trigger_time,
        "timezone": timezone_name,
        "window": window,
        "max_sessions": max_sessions,
    }


def schedule_for(config: Any, tenant: Any) -> dict[str, Any]:
    """Return a tenant's schedule without inheriting the default tenant's job."""
    raw: Any = None
    if tenant is not None and not tenant.is_default():
        overrides = tenant.config_overrides if isinstance(tenant.config_overrides, dict) else {}
        raw = overrides.get("datasource_schedule")
    else:
        raw = getattr(config, "datasource_schedule", None)
    return normalize_schedule(raw)


def validate_schedule_descriptor(descriptor: dict[str, Any]) -> None:
    """Ensure the bound adapter can provide a complete daily time window."""
    if not descriptor.get("configured") or not descriptor.get("enabled"):
        raise ValueError(descriptor.get("error") or "Tenant adapter is disabled")
    supported = set(descriptor.get("supported_filters") or [])
    missing = {"from_timestamp", "to_timestamp"} - supported
    if missing:
        raise ValueError(
            "Scheduled pulls require adapter filters: "
            + ", ".join(sorted(missing))
        )


def previous_day_window(
    schedule: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Build the previous local calendar day's half-open UTC interval."""
    settings = normalize_schedule(schedule)
    tz = ZoneInfo(settings["timezone"])
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(tz)
    target_date = local_now.date() - timedelta(days=1)
    start_local = datetime.combine(target_date, datetime_time.min, tzinfo=tz)
    end_local = datetime.combine(target_date + timedelta(days=1), datetime_time.min, tzinfo=tz)
    scheduled_local = datetime.combine(
        local_now.date(),
        datetime_time.fromisoformat(settings["time"]),
        tzinfo=tz,
    )
    return {
        "target_date": target_date.isoformat(),
        "from_timestamp": _utc_iso(start_local),
        "to_timestamp": _utc_iso(end_local),
        "scheduled_at": _utc_iso(scheduled_local),
    }


def schedule_is_due(
    schedule: dict[str, Any],
    *,
    completed_target_date: str = "",
    now: datetime | None = None,
) -> bool:
    settings = normalize_schedule(schedule)
    if not settings["enabled"]:
        return False
    window = previous_day_window(settings, now=now)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    scheduled_at = datetime.fromisoformat(window["scheduled_at"].replace("Z", "+00:00"))
    return now_utc.astimezone(timezone.utc) >= scheduled_at and completed_target_date != window["target_date"]


def next_run_at(
    schedule: dict[str, Any],
    *,
    completed_target_date: str = "",
    now: datetime | None = None,
) -> str:
    settings = normalize_schedule(schedule)
    if not settings["enabled"]:
        return ""
    tz = ZoneInfo(settings["timezone"])
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(tz)
    trigger = datetime.combine(
        local_now.date(),
        datetime_time.fromisoformat(settings["time"]),
        tzinfo=tz,
    )
    current_target = (local_now.date() - timedelta(days=1)).isoformat()
    if local_now < trigger:
        return _utc_iso(trigger)
    if completed_target_date == current_target:
        return _utc_iso(trigger + timedelta(days=1))
    return _utc_iso(now_utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_file() -> Path:
    explicit = os.environ.get("TEAMEVOLVER_DATASOURCE_SCHEDULE_STATE_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get("TEAMEVOLVER_CONFIG_DIR", "").strip()
    return (Path(root).expanduser() if root else Path.home() / ".teamEvolver") / "datasource_schedule_state.json"


class _ScheduleStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _state_file()
        self._lock = threading.RLock()

    def get(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._read().get(tenant_id) or {})

    def update(self, tenant_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            current = dict(data.get(tenant_id) or {})
            current.update(values)
            data[tenant_id] = current
            self._write(data)
            return dict(current)

    def _read(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


class DatasourcePullRuntime:
    """Own manual pull coordination and the daily pull scheduler."""

    def __init__(
        self,
        owner: Any,
        *,
        invalidate_cache: Callable[..., None] | None = None,
        state_path: Path | None = None,
        max_active: int = 4,
    ) -> None:
        self.owner = owner
        self.invalidate_cache = invalidate_cache
        self._state = _ScheduleStateStore(state_path)
        self._max_active = max(1, int(max_active))
        self._active: set[str] = set()
        self._active_lock = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task] = {}
        self._retry_after: dict[str, float] = {}
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def set_invalidate_cache(self, callback: Callable[..., None] | None) -> None:
        if callback is not None:
            self.invalidate_cache = callback

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("[DatasourceSchedule] scheduler started")

    async def stop(self) -> None:
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None:
            loop_task.cancel()
        jobs = list(self._jobs.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(
            *([loop_task] if loop_task is not None else []),
            *jobs,
            return_exceptions=True,
        )
        self._jobs.clear()
        logger.info("[DatasourceSchedule] scheduler stopped")

    def wake(self) -> None:
        self._wake.set()

    def is_running(self, tenant_id: str) -> bool:
        return tenant_id in self._active or (
            tenant_id in self._jobs and not self._jobs[tenant_id].done()
        )

    async def pull(self, tenant: Any, body: dict[str, Any]) -> dict[str, Any]:
        tenant_id = tenant.tenant_id
        async with self._active_lock:
            if tenant_id in self._active:
                raise PullBusyError("A pull is already running for this tenant")
            if len(self._active) >= self._max_active:
                raise PullCapacityError("Datasource pull capacity reached")
            self._active.add(tenant_id)
        token = set_current_tenant(tenant)
        try:
            return await pull_sessions(
                self.owner.config,
                tenant,
                lambda session: ingest(
                    self.owner,
                    session,
                    invalidate_cache=self.invalidate_cache,
                ),
                body,
            )
        finally:
            reset_current_tenant(token)
            async with self._active_lock:
                self._active.discard(tenant_id)

    async def trigger(self, tenant: Any, *, force: bool = False) -> dict[str, Any]:
        tenant_id = tenant.tenant_id
        if self.is_running(tenant_id):
            raise PullBusyError("A pull is already running for this tenant")
        schedule = schedule_for(self.owner.config, tenant)
        if not schedule["enabled"] and not force:
            raise ValueError("Datasource pull schedule is disabled")
        descriptor = await asyncio.to_thread(adapters.describe, self.owner.config, tenant)
        validate_schedule_descriptor(descriptor)
        window = previous_day_window(schedule)
        self._spawn(tenant, schedule, descriptor, window)
        return await asyncio.to_thread(self.status, tenant)

    def status(self, tenant: Any) -> dict[str, Any]:
        schedule = schedule_for(self.owner.config, tenant)
        state = self._state.get(tenant.tenant_id)
        current_adapter = adapters.binding(self.owner.config, tenant)
        completed_target_date = (
            str(state.get("completed_target_date") or "")
            if str(state.get("adapter_file") or "") == current_adapter
            else ""
        )
        return {
            "running": self.is_running(tenant.tenant_id),
            "next_run_at": next_run_at(
                schedule,
                completed_target_date=completed_target_date,
            ),
            "last_started_at": str(state.get("last_started_at") or ""),
            "last_finished_at": str(state.get("last_finished_at") or ""),
            "last_status": str(state.get("last_status") or ""),
            "last_error": str(state.get("last_error") or ""),
            "last_window_from": str(state.get("last_window_from") or ""),
            "last_window_to": str(state.get("last_window_to") or ""),
            "last_target_date": str(state.get("target_date") or ""),
            "last_total": int(state.get("last_total") or 0),
            "last_counts": dict(state.get("last_counts") or {}),
        }

    async def _run_loop(self) -> None:
        tick = self._tick_seconds()
        try:
            while True:
                try:
                    await self._scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[DatasourceSchedule] scheduler pass failed")
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=tick)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _scan_once(self, *, now: datetime | None = None) -> None:
        from teamEvolver.proxy.tenant_routes import get_tenant_registry

        registry = get_tenant_registry(self.owner)
        tenants = await asyncio.to_thread(registry.list_tenants)
        monotonic_now = asyncio.get_running_loop().time()
        for tenant in tenants:
            if tenant.status != "active" or self.is_running(tenant.tenant_id):
                continue
            if monotonic_now < self._retry_after.get(tenant.tenant_id, 0):
                continue
            try:
                schedule = schedule_for(self.owner.config, tenant)
                if not schedule["enabled"]:
                    continue
                descriptor = await asyncio.to_thread(
                    adapters.describe,
                    self.owner.config,
                    tenant,
                )
                validate_schedule_descriptor(descriptor)
                window = previous_day_window(schedule, now=now)
                state = await asyncio.to_thread(self._state.get, tenant.tenant_id)
                completed = (
                    str(state.get("completed_target_date") or "") == window["target_date"]
                    and str(state.get("adapter_file") or "") == descriptor["file"]
                )
                if not schedule_is_due(
                    schedule,
                    completed_target_date=window["target_date"] if completed else "",
                    now=now,
                ):
                    continue
                self._spawn(tenant, schedule, descriptor, window)
            except Exception as exc:
                logger.warning(
                    "[DatasourceSchedule] tenant %s schedule skipped: %s",
                    tenant.tenant_id,
                    exc,
                )

    def _spawn(
        self,
        tenant: Any,
        schedule: dict[str, Any],
        descriptor: dict[str, Any],
        window: dict[str, str],
    ) -> None:
        tenant_id = tenant.tenant_id
        if self.is_running(tenant_id):
            return
        task = asyncio.create_task(
            self._run_scheduled(tenant, schedule, descriptor, window)
        )
        self._jobs[tenant_id] = task

        def done(completed: asyncio.Task) -> None:
            self._jobs.pop(tenant_id, None)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None:
                logger.error(
                    "[DatasourceSchedule] tenant %s job failed: %s",
                    tenant_id,
                    exc,
                    exc_info=exc,
                )

        task.add_done_callback(done)

    async def _run_scheduled(
        self,
        tenant: Any,
        schedule: dict[str, Any],
        descriptor: dict[str, Any],
        window: dict[str, str],
    ) -> None:
        tenant_id = tenant.tenant_id
        registry = None
        runtime = None
        advisory_key = f"datasource:{tenant_id}"
        locked = False
        try:
            from teamEvolver.proxy.tenant_routes import get_tenant_registry

            registry = get_tenant_registry(self.owner)
            runtime = getattr(registry, "runtime", None)
            if runtime is not None:
                locked = await asyncio.to_thread(
                    runtime.try_advisory_lock,
                    advisory_key,
                )
                if not locked:
                    self._retry_after[tenant_id] = (
                        asyncio.get_running_loop().time() + 30.0
                    )
                    return

            started_at = _utc_iso(datetime.now(timezone.utc))
            await asyncio.to_thread(
                self._state.update,
                tenant_id,
                {
                    "adapter_file": descriptor["file"],
                    "target_date": window["target_date"],
                    "last_started_at": started_at,
                    "last_status": "running",
                    "last_error": "",
                    "last_window_from": window["from_timestamp"],
                    "last_window_to": window["to_timestamp"],
                },
            )
            result = await self.pull(
                tenant,
                {
                    "from_timestamp": window["from_timestamp"],
                    "to_timestamp": window["to_timestamp"],
                    "max_sessions": schedule["max_sessions"],
                    "defer_evolution_trigger": True,
                },
            )
            counts = dict(result.get("counts") or {})
            status = "partial" if int(counts.get("error") or 0) else "succeeded"
            await asyncio.to_thread(
                self._state.update,
                tenant_id,
                {
                    "completed_target_date": window["target_date"],
                    "last_finished_at": _utc_iso(datetime.now(timezone.utc)),
                    "last_status": status,
                    "last_error": "",
                    "last_total": int(result.get("total") or 0),
                    "last_counts": counts,
                },
            )
            self._retry_after.pop(tenant_id, None)
            logger.info(
                "[DatasourceSchedule] tenant %s pulled %s (%s)",
                tenant_id,
                window["target_date"],
                counts,
            )
        except asyncio.CancelledError:
            raise
        except (PullBusyError, PullCapacityError) as exc:
            self._retry_after[tenant_id] = asyncio.get_running_loop().time() + 30.0
            await asyncio.to_thread(
                self._state.update,
                tenant_id,
                {
                    "last_finished_at": _utc_iso(datetime.now(timezone.utc)),
                    "last_status": "deferred",
                    "last_error": str(exc),
                },
            )
            logger.info("[DatasourceSchedule] tenant %s deferred: %s", tenant_id, exc)
        except Exception as exc:
            retry_seconds = self._retry_seconds()
            self._retry_after[tenant_id] = asyncio.get_running_loop().time() + retry_seconds
            await asyncio.to_thread(
                self._state.update,
                tenant_id,
                {
                    "last_finished_at": _utc_iso(datetime.now(timezone.utc)),
                    "last_status": "failed",
                    "last_error": str(exc)[:1000],
                },
            )
            logger.warning(
                "[DatasourceSchedule] tenant %s pull failed; retry in %ss: %s",
                tenant_id,
                retry_seconds,
                exc,
                exc_info=True,
            )
        finally:
            if locked and runtime is not None:
                await asyncio.to_thread(runtime.release_advisory_lock, advisory_key)

    @staticmethod
    def _tick_seconds() -> float:
        try:
            return max(
                5.0,
                float(
                    os.environ.get(
                        "TEAMEVOLVER_DATASOURCE_SCHEDULE_TICK_S",
                        "30",
                    )
                ),
            )
        except ValueError:
            return 30.0

    @staticmethod
    def _retry_seconds() -> float:
        try:
            return max(
                30.0,
                float(
                    os.environ.get(
                        "TEAMEVOLVER_DATASOURCE_SCHEDULE_RETRY_S",
                        "300",
                    )
                ),
            )
        except ValueError:
            return 300.0


def get_pull_runtime(
    owner: Any,
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> DatasourcePullRuntime:
    runtime = getattr(owner, "_datasource_pull_runtime", None)
    if runtime is None:
        runtime = DatasourcePullRuntime(
            owner,
            invalidate_cache=invalidate_cache,
        )
        owner._datasource_pull_runtime = runtime
    else:
        runtime.set_invalidate_cache(invalidate_cache)
    return runtime


__all__ = [
    "DEFAULT_SCHEDULE",
    "DatasourcePullRuntime",
    "PullBusyError",
    "PullCapacityError",
    "get_pull_runtime",
    "next_run_at",
    "normalize_schedule",
    "previous_day_window",
    "schedule_for",
    "schedule_is_due",
    "validate_schedule_descriptor",
]
