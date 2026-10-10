"""Compile-only Team Memory maintenance runtime.

Maintenance is owned entirely by OpenViking Compile:

1. copy the current Team Memory to a private snapshot with ``ov cp``;
2. run the configured maintenance Skill with ``ov compile``;
3. publish the result back to the account-shared Team Memory directory.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from team_memory.memory_changes import MemoryChangeLedger
from team_memory.service import MemoryAggregationService
from team_replay.memory import MemoryTrueReplayRunner

logger = logging.getLogger(__name__)


class TeamMemorySupervisor:
    """Schedule compile-only Team Memory maintenance."""

    def __init__(self, config: Any, service: MemoryAggregationService | None = None):
        self.config = config
        self._team_memory = service or MemoryAggregationService(config)
        self._stop_event = threading.Event()
        self._daemon_thread: threading.Thread | None = None
        self._trigger_thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._last_results: list[dict[str, Any]] = []
        self._last_error = ""
        self._ledger: MemoryChangeLedger | None = None
        self._memory_replay: MemoryTrueReplayRunner | None = None

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "memory_maintenance_enabled", False))

    def _missing(self) -> list[str]:
        values = {
            "sharing.viking_endpoint": getattr(
                self.config, "sharing_viking_endpoint", ""
            ),
            "sharing.viking_api_key": getattr(
                self.config, "sharing_viking_api_key", ""
            ),
        }
        return [
            key
            for key, value in values.items()
            if not str(value or "").strip()
        ]

    def start(self) -> dict[str, Any]:
        if (
            not self._enabled()
            or not bool(
                getattr(self.config, "memory_maintenance_auto_start", False)
            )
            or self._missing()
        ):
            return self.status()
        if self._daemon_thread and self._daemon_thread.is_alive():
            return self.status()
        self._stop_event.clear()
        self._daemon_thread = threading.Thread(
            target=self._daemon_loop,
            name="team-memory-compile-daemon",
            daemon=True,
        )
        self._daemon_thread.start()
        return self.status()

    def trigger(self) -> dict[str, Any]:
        if not self._enabled():
            return {"status": "disabled", **self.status()}
        missing = self._missing()
        if missing:
            return {
                "status": "not_configured",
                "missing": missing,
                **self.status(),
            }
        if self._run_lock.locked():
            return {"status": "already_running", **self.status()}
        self._trigger_thread = threading.Thread(
            target=self._run_once,
            name="team-memory-compile-once",
            daemon=True,
        )
        self._trigger_thread.start()
        return {"status": "started", **self.status()}

    def _run_once(self) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        run = None
        try:
            account = str(
                getattr(self.config, "sharing_viking_account", "") or "default"
            )
            key = str(
                getattr(self.config, "sharing_viking_api_key", "") or ""
            ).strip()
            run = self._team_memory.new_run(account, pipeline="maintain")
            asyncio.run(self._team_memory.reconcile(run, key))
            self._team_memory.run(run, api_key=key)
            self._last_results = [run.to_public()]
            self._last_error = str(run.error or "")
        except Exception as exc:  # noqa: BLE001 - surfaced through status.
            self._last_error = str(exc)
            if run is not None and run.status == "pending":
                self._team_memory.forget_run(run.task_id)
            logger.exception("[TeamMemory] compile maintenance failed")
        finally:
            self._run_lock.release()

    def _daemon_loop(self) -> None:
        interval = max(
            60,
            int(
                getattr(
                    self.config,
                    "memory_maintenance_interval_seconds",
                    86400,
                )
                or 86400
            ),
        )
        while not self._stop_event.is_set():
            self._run_once()
            self._stop_event.wait(interval)

    def dry_run(self) -> dict[str, Any]:
        return {
            "engine": "team-memory-compile",
            "maintained_space": self._team_memory.resolve_target_uri(),
            "stages": ["snapshot", "maintenance"],
            "skill_uri": self._team_memory._shared_skill_uri("maintenance"),
            "tools": ["ov_cp", "ov_compile"],
        }

    def status(self) -> dict[str, Any]:
        missing = self._missing()
        return {
            "engine": "team-memory-compile",
            "full_capabilities": False,
            "enabled": self._enabled(),
            "configured": not missing,
            "missing": missing,
            "running": self._run_lock.locked(),
            "pid": None,
            "daemon_running": bool(
                self._daemon_thread and self._daemon_thread.is_alive()
            ),
            "daemon_pid": None,
            "maintained_space": self._team_memory.resolve_target_uri(),
            "tools": ["ov_cp", "ov_compile"],
            "last_results": self._last_results,
            "last_error": self._last_error,
            "runs": self._team_memory.list_runs(),
        }

    def _tenant_ledger(self, config: Any) -> MemoryChangeLedger:
        return MemoryChangeLedger.from_config(config)

    def _default_ledger(self) -> MemoryChangeLedger:
        if self._ledger is None:
            self._ledger = MemoryChangeLedger.from_config(self.config)
        return self._ledger

    def memory_changes(
        self,
        *,
        limit: int = 100,
        config: Any = None,
    ) -> dict[str, Any]:
        ledger = (
            self._tenant_ledger(config)
            if config is not None
            else self._default_ledger()
        )
        changes = ledger.list_changes(limit=max(1, min(500, int(limit))))
        replays = ledger.list_replays(limit=10000)
        latest_by_change: dict[str, dict[str, Any]] = {}
        for replay in replays:
            change_id = str(replay.get("change_id") or "")
            if change_id and change_id not in latest_by_change:
                latest_by_change[change_id] = replay
        for change in changes:
            latest = latest_by_change.get(str(change.get("change_id") or ""))
            if latest is not None:
                change["latest_replay"] = latest
        return {
            "schema_version": "teamevolver.memory-change-list.v1",
            "count": len(changes),
            "items": changes,
        }

    def _replay_runner(self, config: Any = None) -> MemoryTrueReplayRunner:
        if config is not None:
            return MemoryTrueReplayRunner(
                ledger=self._tenant_ledger(config),
            )
        if self._memory_replay is None:
            self._memory_replay = MemoryTrueReplayRunner(
                ledger=self._default_ledger(),
            )
        return self._memory_replay

    def run_memory_replay(
        self,
        *,
        change_id: str,
        query: str,
        checklist: list[Any],
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
        config: Any = None,
    ) -> dict[str, Any]:
        return self._replay_runner(config).run(
            change_id=change_id,
            query=query,
            checklist=checklist,
            source_session_id=source_session_id,
            max_interactions=max_interactions,
            timeout_seconds=timeout_seconds,
        )

    def run_memory_replay_adhoc(
        self,
        *,
        memory_path: str,
        before_content: str,
        after_content: str,
        query: str,
        checklist: list[Any],
        scope: str = "team_memory",
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
        config: Any = None,
    ) -> dict[str, Any]:
        return self._replay_runner(config).run_adhoc(
            memory_path=memory_path,
            before_content=before_content,
            after_content=after_content,
            query=query,
            checklist=checklist,
            scope=scope,
            source_session_id=source_session_id,
            max_interactions=max_interactions,
            timeout_seconds=timeout_seconds,
        )

    def memory_replays(
        self,
        *,
        change_id: str,
        limit: int = 100,
        config: Any = None,
    ) -> dict[str, Any]:
        items = self._replay_runner(config).list_replays(
            change_id=change_id,
            limit=max(1, min(500, int(limit))),
        )
        return {
            "schema_version": "teamevolver.memory-true-replay-list.v1",
            "change_id": change_id,
            "count": len(items),
            "items": items,
        }

    def stop(self) -> None:
        self._stop_event.set()
        for thread in (self._daemon_thread, self._trigger_thread):
            if (
                thread
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=10)
        if self._ledger is not None:
            self._ledger.close()
