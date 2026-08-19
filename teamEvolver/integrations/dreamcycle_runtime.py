"""Full DreamCycle runtime embedded natively in teamEvolver."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..dreamcycle.config import (
    DreamCycleConfig,
    LLMConfig,
    LogConfig,
    OpenVikingConfig,
    SchedulerConfig,
)
from ..dreamcycle.jobs import ALL_JOBS
from ..dreamcycle.logging_config import setup_embedded_logging
from ..dreamcycle.memory_replay import MemoryTrueReplayRunner
from ..dreamcycle.scheduler import Scheduler
from .dreamcycle import (
    collect_personal_source_keys,
    collect_personal_source_users,
    parse_openviking_key,
)

logger = logging.getLogger(__name__)

_STATE_DIR = Path.home() / ".teamEvolver" / "dreamcycle"
_JOB_CLASSES = {job().name: job for job in ALL_JOBS}
_TOOL_NAMES = [
    "viking_search",
    "viking_read",
    "viking_read_many",
    "viking_browse",
    "viking_remember",
    "viking_forget",
    "viking_merge",
    "list_customers",
    "memory_audit",
    "memory_sanitize",
    "save_report",
    "shared_notes",
]


def available_jobs(team_name: str = "") -> list[dict[str, Any]]:
    """Return the complete native DreamCycle job catalog."""
    return [
        {
            "id": job.name,
            "label": {
                "team_overview": "团队概况维护",
                "deduplication": "团队 Memory 去重",
                "cleanup": "过期 Memory 清理",
                "onboarding_check": "新人可发现性检查",
                "consolidate": "个人经验团队化",
            }.get(job.name, job.name),
            "description": job.description,
            "priority": job.priority,
            "default_prompt": job.get_system_prompt(),
            "tasks": [
                {
                    "id": task.id,
                    "description": task.description,
                    "priority": task.priority.value,
                }
                for task in job.create_plan().tasks
            ],
        }
        for job in sorted(
            (job_class(team_name=team_name) for job_class in ALL_JOBS),
            key=lambda item: item.priority,
        )
    ]


class FullDreamCycleSupervisor:
    """Own the complete DreamCycle scheduler inside the teamEvolver process."""

    def __init__(self, config: Any):
        self.config = config
        self._stop_event = threading.Event()
        self._daemon_thread: threading.Thread | None = None
        self._trigger_thread: threading.Thread | None = None
        self._round_lock = threading.Lock()
        self._last_results: list[dict[str, Any]] = []
        self._last_error = ""
        self._scheduler = self._build_scheduler()
        self._memory_replay = MemoryTrueReplayRunner(
            ledger=self._scheduler._change_ledger,
            app_config=self.config,
        )

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "dreamcycle_enabled", False))

    def _missing(self) -> list[str]:
        values = {
            "sharing.viking_endpoint": getattr(self.config, "sharing_viking_endpoint", ""),
            "sharing.viking_team_api_key": (
                getattr(self.config, "sharing_viking_team_api_key", "")
                or getattr(self.config, "sharing_viking_api_key", "")
            ),
            "dreamcycle.llm_api_key": (
                getattr(self.config, "dreamcycle_llm_api_key", "")
                or getattr(self.config, "llm_api_key", "")
            ),
            "dreamcycle.llm_model": (
                getattr(self.config, "dreamcycle_llm_model", "")
                or getattr(self.config, "llm_model_id", "")
            ),
        }
        return [
            key
            for key, value in values.items()
            if not str(value or "").strip()
        ]

    def _selected_job_classes(self) -> list[type]:
        configured = getattr(self.config, "dreamcycle_enabled_jobs", None)
        if isinstance(configured, (list, tuple, set)):
            selected = [str(item) for item in configured]
        else:
            selected = list(_JOB_CLASSES)
        return [
            _JOB_CLASSES[job_id]
            for job_id in selected
            if job_id in _JOB_CLASSES
        ]

    def _build_scheduler(self) -> Scheduler:
        state_dir = Path(
            str(
                getattr(self.config, "dreamcycle_state_dir", "")
                or _STATE_DIR
            )
        ).expanduser()
        team_key = str(
            getattr(self.config, "sharing_viking_team_api_key", "")
            or getattr(self.config, "sharing_viking_api_key", "")
            or ""
        ).strip()
        account, encoded_team_user = parse_openviking_key(team_key)
        dreamcycle_config = DreamCycleConfig(
            viking=OpenVikingConfig(
                endpoint=str(
                    getattr(self.config, "sharing_viking_endpoint", "") or ""
                ),
                api_key=team_key,
                account=(
                    account
                    or str(
                        getattr(
                            self.config,
                            "sharing_viking_account",
                            "",
                        )
                        or "default"
                    )
                ),
                agent_id=(
                    encoded_team_user
                    or str(
                        getattr(
                            self.config,
                            "sharing_viking_user",
                            "",
                        )
                        or "default"
                    )
                ),
                source_api_keys=collect_personal_source_keys(self.config),
                source_users=collect_personal_source_users(self.config),
                agent=str(
                    getattr(
                        self.config,
                        "dreamcycle_viking_agent",
                        "",
                    )
                    or "teamEvolver-dreamcycle"
                ),
                customer_id=str(
                    getattr(self.config, "dreamcycle_customer_id", "") or ""
                ),
            ),
            llm=LLMConfig(
                base_url=str(
                    getattr(self.config, "dreamcycle_llm_base_url", "")
                    or getattr(self.config, "llm_api_base", "")
                    or ""
                ),
                api_key=str(
                    getattr(self.config, "dreamcycle_llm_api_key", "")
                    or getattr(self.config, "llm_api_key", "")
                    or ""
                ),
                model=str(
                    getattr(self.config, "dreamcycle_llm_model", "")
                    or getattr(self.config, "llm_model_id", "")
                    or ""
                ),
                max_tokens=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_llm_max_tokens",
                            4096,
                        )
                        or 4096
                    ),
                ),
                temperature=max(
                    0.0,
                    min(
                        2.0,
                        float(
                            getattr(
                                self.config,
                                "dreamcycle_temperature",
                                0.3,
                            )
                            or 0.3
                        ),
                    ),
                ),
                embed_model=str(
                    getattr(self.config, "dreamcycle_embed_model", "") or ""
                ),
                embed_base_url=str(
                    getattr(self.config, "dreamcycle_embed_base_url", "") or ""
                ),
                embed_api_key=str(
                    getattr(self.config, "dreamcycle_embed_api_key", "") or ""
                ),
                dedup_merge_threshold=max(
                    -1.0,
                    min(
                        1.0,
                        float(
                            getattr(
                                self.config,
                                "dreamcycle_dedup_merge_threshold",
                                0.86,
                            )
                        ),
                    ),
                ),
                dedup_warn_threshold=max(
                    -1.0,
                    min(
                        1.0,
                        float(
                            getattr(
                                self.config,
                                "dreamcycle_dedup_warn_threshold",
                                0.72,
                            )
                        ),
                    ),
                ),
            ),
            scheduler=SchedulerConfig(
                active_start_hour=int(
                    getattr(
                        self.config,
                        "dreamcycle_active_start_hour",
                        0,
                    )
                    or 0
                ),
                active_end_hour=int(
                    (
                        getattr(
                            self.config,
                            "dreamcycle_active_end_hour",
                            6,
                        )
                        if getattr(
                            self.config,
                            "dreamcycle_active_end_hour",
                            None,
                        )
                        is not None
                        else 6
                    )
                ),
                rounds_per_window=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_rounds_per_window",
                            3,
                        )
                        or 3
                    ),
                ),
                round_interval_minutes=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_round_interval_minutes",
                            90,
                        )
                        or 90
                    ),
                ),
                max_turns_per_job=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_max_turns_per_job",
                            25,
                        )
                        or 25
                    ),
                ),
                max_consecutive_errors=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_max_consecutive_errors",
                            3,
                        )
                        or 3
                    ),
                ),
                retry_delay_seconds=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "dreamcycle_retry_delay_seconds",
                            300,
                        )
                        or 300
                    ),
                ),
            ),
            log=LogConfig(
                log_dir=state_dir / "logs",
                report_dir=state_dir / "reports",
                state_file=state_dir / "state.json",
                log_level=str(
                    getattr(self.config, "dreamcycle_log_level", "") or "INFO"
                ),
            ),
            job_prompts=dict(
                getattr(self.config, "dreamcycle_job_prompts", {}) or {}
            ),
            job_settings=dict(
                getattr(self.config, "dreamcycle_job_settings", {}) or {}
            ),
            team_name=str(
                getattr(self.config, "team_display_name", "") or "Team"
            ),
        )
        setup_embedded_logging(dreamcycle_config.log)
        return Scheduler(
            dreamcycle_config,
            self._selected_job_classes(),
            register_signals=False,
        )

    def start(self) -> dict[str, Any]:
        if (
            not self._enabled()
            or not bool(
                getattr(self.config, "dreamcycle_auto_start", False)
            )
            or self._missing()
        ):
            return self.status()
        if self._daemon_thread and self._daemon_thread.is_alive():
            return self.status()
        self._stop_event.clear()
        self._daemon_thread = threading.Thread(
            target=self._daemon_loop,
            name="teamEvolver-dreamcycle-daemon",
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
        if self._round_lock.locked():
            return {"status": "already_running", **self.status()}
        self._trigger_thread = threading.Thread(
            target=self._run_round,
            name="teamEvolver-dreamcycle-once",
            daemon=True,
        )
        self._trigger_thread.start()
        return {"status": "started", **self.status()}

    def _run_round(self) -> None:
        if not self._round_lock.acquire(blocking=False):
            return
        try:
            results = self._scheduler.run_once()
            self._last_results = [
                result.to_dict()
                for result in results
            ]
            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("[DreamCycle] native round failed")
        finally:
            self._round_lock.release()

    def _daemon_loop(self) -> None:
        scheduler_config = self._scheduler._config.scheduler
        while not self._stop_event.is_set():
            if (
                self._scheduler.is_active_window()
                and self._scheduler._state.should_run_round(
                    scheduler_config.rounds_per_window
                )
            ):
                self._run_round()
                wait_seconds = (
                    scheduler_config.round_interval_minutes * 60
                )
            else:
                wait_seconds = 1800
            self._stop_event.wait(wait_seconds)

    def dry_run(self) -> dict[str, Any]:
        """Return every enabled job and its full plan without side effects."""
        enabled = {
            job_class().name
            for job_class in self._selected_job_classes()
        }
        return {
            "engine": "teamEvolver-native-dreamcycle",
            "maintained_space": (
                f"viking://user/peers/{self._scheduler._config.viking.customer_id}/memories/"
                if self._scheduler._config.viking.customer_id
                else "viking://user/memories/"
            ),
            "agent_id": self._scheduler._config.viking.agent_id,
            "semantic_dedup_enabled": (
                bool(self._scheduler._config.llm.embed_model)
                and bool(self._scheduler._config.llm.embed_api_key)
            ),
            "tools": list(_TOOL_NAMES),
            "jobs": [
                {
                    **job,
                    "enabled": job["id"] in enabled,
                    "effective_prompt": (
                        getattr(
                            self.config,
                            "dreamcycle_job_prompts",
                            {},
                        )
                        or {}
                    ).get(job["id"])
                    or job["default_prompt"],
                    "runtime": self._effective_job_runtime(job["id"]),
                    "default_runtime": self._default_job_runtime(),
                    "settings_overridden": bool(
                        (
                            getattr(
                                self.config,
                                "dreamcycle_job_settings",
                                {},
                            )
                            or {}
                        ).get(job["id"])
                    ),
                }
                for job in available_jobs(
                    str(getattr(self.config, "team_display_name", "") or "Team")
                )
            ],
        }

    def memory_changes(self, *, limit: int = 100) -> dict[str, Any]:
        changes = self._scheduler._change_ledger.list_changes(
            limit=max(1, min(500, int(limit))),
        )
        latest_by_change: dict[str, dict[str, Any]] = {}
        for replay in self._memory_replay.list_replays(limit=10000):
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

    def run_memory_replay(
        self,
        *,
        change_id: str,
        query: str,
        checklist: list[Any],
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        return self._memory_replay.run(
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
    ) -> dict[str, Any]:
        return self._memory_replay.run_adhoc(
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
    ) -> dict[str, Any]:
        items = self._memory_replay.list_replays(
            change_id=change_id,
            limit=max(1, min(500, int(limit))),
        )
        return {
            "schema_version": "teamevolver.memory-true-replay-list.v1",
            "change_id": change_id,
            "count": len(items),
            "items": items,
        }

    def reset(
        self,
        *,
        remote: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview or reset local state and the maintained memory space."""
        if self._round_lock.locked():
            return {
                "status": "running",
                "error": "DreamCycle is currently running",
            }

        from ..dreamcycle.reset import reset

        output = reset(
            self._scheduler._config,
            remote=remote,
            dry_run=dry_run,
        )
        if not dry_run:
            from ..dreamcycle.scheduler import ExecutionState

            self._scheduler._state = ExecutionState(
                self._scheduler._config.log.state_file
            )
            self._last_results = []
            self._last_error = ""
        return {
            "status": "preview" if dry_run else "reset",
            "remote": remote,
            "dry_run": dry_run,
            "output": output,
        }

    def _default_job_runtime(self) -> dict[str, Any]:
        return {
            "model": "",
            "base_url": "",
            "temperature": float(
                getattr(self.config, "dreamcycle_temperature", 0.3)
                or 0.3
            ),
            "max_tokens": int(
                getattr(
                    self.config,
                    "dreamcycle_llm_max_tokens",
                    4096,
                )
                or 4096
            ),
            "max_turns": int(
                getattr(
                    self.config,
                    "dreamcycle_max_turns_per_job",
                    25,
                )
                or 25
            ),
            "max_errors": int(
                getattr(
                    self.config,
                    "dreamcycle_max_consecutive_errors",
                    3,
                )
                or 3
            ),
        }

    def _effective_job_runtime(self, job_id: str) -> dict[str, Any]:
        defaults = self._default_job_runtime()
        override = (
            getattr(self.config, "dreamcycle_job_settings", {}) or {}
        ).get(job_id) or {}
        return {**defaults, **override}

    def status(self) -> dict[str, Any]:
        scheduler_status = self._scheduler.status()
        runtime_config = self._scheduler._config
        return {
            "engine": "teamEvolver-native-dreamcycle",
            "full_capabilities": True,
            "enabled": self._enabled(),
            "configured": not self._missing(),
            "missing": self._missing(),
            "running": self._round_lock.locked(),
            "pid": None,
            "daemon_running": bool(
                self._daemon_thread
                and self._daemon_thread.is_alive()
            ),
            "daemon_pid": None,
            "log_file": str(
                self._scheduler._config.log.log_dir
                / "dreamcycle.log"
            ),
            "daemon_log_file": str(
                self._scheduler._config.log.log_dir
                / "dreamcycle.log"
            ),
            "report_dir": str(
                runtime_config.log.report_dir
            ),
            "agent_id": runtime_config.viking.agent_id,
            "customer_id": runtime_config.viking.customer_id,
            "maintained_space": (
                f"viking://user/peers/{runtime_config.viking.customer_id}/memories/"
                if runtime_config.viking.customer_id
                else "viking://user/memories/"
            ),
            "semantic_dedup_enabled": (
                bool(runtime_config.llm.embed_model)
                and bool(runtime_config.llm.embed_api_key)
            ),
            "tools": list(_TOOL_NAMES),
            "last_results": self._last_results,
            "last_error": self._last_error,
            **scheduler_status,
        }

    def stop(self) -> None:
        self._stop_event.set()
        self._scheduler.shutdown()
        for thread in (
            self._daemon_thread,
            self._trigger_thread,
        ):
            if (
                thread
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=10)
        self._scheduler._change_ledger.close()
