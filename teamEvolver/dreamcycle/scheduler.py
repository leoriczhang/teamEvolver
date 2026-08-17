"""Scheduler — time-window based job execution with state persistence.

Runs multiple rounds per night window, with:
- Configurable active hours (default: 0:00-6:00)
- Multiple rounds per window (default: 3, every 90 min)
- Job-level error isolation (one job failing doesn't block others)
- Persistent state tracking (last run, history)
- Graceful shutdown support
"""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

from .config import DreamCycleConfig
from .jobs.base import Job, JobResult, JobStatus
from .memory_changes import MemoryChangeLedger
from .react.engine import ReActEngine
from .tools.base import ToolRegistry

if TYPE_CHECKING:
    from .blackboard import Blackboard

logger = logging.getLogger(__name__)


class ExecutionState:
    """Persistent state across runs — tracks history and prevents re-runs."""

    def __init__(self, state_file: Path):
        self._file = state_file
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text())
            except Exception:
                return {}
        return {}

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(self._data, indent=2, ensure_ascii=False, default=str))

    @property
    def last_run_date(self) -> Optional[str]:
        return self._data.get("last_run_date")

    @property
    def total_cycles(self) -> int:
        return self._data.get("total_cycles", 0)

    @property
    def rounds_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._data.get("last_run_date") == today:
            return self._data.get("rounds_today", 0)
        return 0

    @property
    def history(self) -> List[Dict[str, Any]]:
        return list(self._data.get("history") or [])

    def record_round(self, results: List[JobResult]) -> None:
        """Record a completed round."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._data.get("last_run_date") != today:
            self._data["rounds_today"] = 0
        self._data["last_run_date"] = today
        self._data["last_run_time"] = datetime.now(timezone.utc).isoformat()
        self._data["rounds_today"] = self._data.get("rounds_today", 0) + 1
        self._data["total_cycles"] = self._data.get("total_cycles", 0) + 1

        # Keep last 30 days of history
        history = self._data.setdefault("history", [])
        history.append({
            "date": today,
            "time": datetime.now(timezone.utc).isoformat(),
            "round": self._data["rounds_today"],
            "jobs": [r.to_dict() for r in results],
        })
        # Trim old history
        if len(history) > 90:
            self._data["history"] = history[-90:]

        self.save()

    def should_run_round(self, max_rounds: int) -> bool:
        """Check if another round should run today."""
        return self.rounds_today < max_rounds


class Scheduler:
    """Manages the DreamCycle execution schedule.
    
    Modes:
    - daemon: Runs continuously, executing during active hours
    - once: Runs a single round immediately (ignores time window)
    - round: Runs one round only if within time window
    """

    def __init__(
        self,
        config: DreamCycleConfig,
        jobs: List[Type[Job]],
        *,
        register_signals: bool = True,
        change_ledger: MemoryChangeLedger | None = None,
    ):
        self._config = config
        self._job_classes = sorted(jobs, key=lambda j: j().priority if hasattr(j, 'priority') else 50)
        self._state = ExecutionState(config.log.state_file)
        self._change_ledger = (
            change_ledger
            if change_ledger is not None
            else MemoryChangeLedger.from_config(config.viking)
        )
        self._shutdown = False
        self._signal_count = 0

        if register_signals:
            signal.signal(signal.SIGTERM, self._handle_shutdown)
            signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        self._signal_count += 1
        if self._signal_count >= 2:
            logger.info("Force shutdown (signal #%d)", self._signal_count)
            import os
            os._exit(1)
        logger.info("Shutdown signal received — press Ctrl+C again to force quit")
        self._shutdown = True

    def is_active_window(self) -> bool:
        """Check if current time is within the active maintenance window."""
        hour = datetime.now().hour
        start = self._config.scheduler.active_start_hour
        end = self._config.scheduler.active_end_hour
        if start < end:
            return start <= hour < end
        else:
            # Wraps midnight (e.g., 22:00-06:00)
            return hour >= start or hour < end

    def _build_tools(self, blackboard: "Blackboard | None" = None) -> ToolRegistry:
        """Build the tool registry with all available tools."""
        if blackboard is None:
            from .blackboard import Blackboard

            blackboard = Blackboard()

        from .tools.blackboard_tool import BlackboardTool
        from .tools.policy import MemoryAuditTool, MemorySanitizeTool
        from .tools.report import SaveReportTool
        from .tools.viking import (
            ListCustomersTool,
            VikingBrowseTool,
            VikingForgetTool,
            VikingHTTPClient,
            VikingMergeTool,
            VikingReadManyTool,
            VikingReadTool,
            VikingRememberTool,
            VikingSearchTool,
        )

        client = VikingHTTPClient(self._config.viking)
        source_clients = [
            VikingHTTPClient(self._config.viking, api_key_override=api_key)
            for api_key in self._config.viking.source_api_keys
        ]
        source_clients.extend(
            VikingHTTPClient(self._config.viking, user_override=user)
            for user in self._config.viking.source_users
        )
        source_clients = list(
            {
                source.user: source
                for source in source_clients
                if source.user and source.user != client.user
            }.values()
        )
        registry = ToolRegistry()

        customer_id = self._config.viking.customer_id
        matcher = self._config.build_semantic_matcher()
        if not matcher.enabled:
            logger.warning(
                "[Scheduler] semantic dedup disabled (no DREAMCYCLE_EMBED_MODEL); "
                "duplicate detection is deferred to the LLM, not lexical overlap"
            )

        registry.register(
            VikingSearchTool(
                client,
                customer_id,
                source_clients=source_clients,
            )
        )
        registry.register(VikingReadTool(client, source_clients=source_clients))
        registry.register(VikingReadManyTool(client, source_clients=source_clients))
        registry.register(VikingBrowseTool(client, customer_id))
        registry.register(
            VikingRememberTool(
                client,
                customer_id,
                matcher=matcher,
                team_name=self._config.team_name,
                change_ledger=self._change_ledger,
            )
        )
        registry.register(
            VikingForgetTool(
                client,
                customer_id,
                blackboard=blackboard,
                change_ledger=self._change_ledger,
            )
        )
        registry.register(
            VikingMergeTool(
                client,
                customer_id,
                blackboard=blackboard,
                team_name=self._config.team_name,
                change_ledger=self._change_ledger,
            )
        )
        registry.register(ListCustomersTool(client, source_clients=source_clients))
        registry.register(MemoryAuditTool(client, customer_id, matcher=matcher))
        registry.register(
            MemorySanitizeTool(
                client,
                customer_id,
                team_name=self._config.team_name,
                change_ledger=self._change_ledger,
            )
        )
        registry.register(SaveReportTool(self._config.log.report_dir))
        registry.register(BlackboardTool(blackboard))

        return registry

    def _job_config(self, job_name: str) -> DreamCycleConfig:
        """Apply one Job's model and ReAct overrides without mutating globals."""
        settings = self._config.job_settings.get(job_name) or {}
        llm_updates: Dict[str, Any] = {}
        scheduler_updates: Dict[str, Any] = {}

        for key in ("model", "base_url"):
            value = str(settings.get(key) or "").strip()
            if value:
                llm_updates[key] = value
        for key in ("temperature", "max_tokens"):
            if settings.get(key) is not None:
                llm_updates[key] = settings[key]
        if settings.get("max_turns") is not None:
            scheduler_updates["max_turns_per_job"] = settings["max_turns"]
        if settings.get("max_errors") is not None:
            scheduler_updates["max_consecutive_errors"] = settings["max_errors"]

        return DreamCycleConfig(
            viking=self._config.viking,
            llm=self._config.llm.model_copy(update=llm_updates),
            scheduler=self._config.scheduler.model_copy(update=scheduler_updates),
            log=self._config.log,
            job_prompts=self._config.job_prompts,
            job_settings=self._config.job_settings,
            team_name=self._config.team_name,
        )

    def run_round(self) -> List[JobResult]:
        """Execute one full round of all maintenance jobs."""
        logger.info("=" * 60)
        logger.info("DreamCycle Round %d starting", self._state.rounds_today + 1)
        logger.info("=" * 60)

        from .blackboard import Blackboard

        # One shared blackboard per round: jobs run on fresh engines but reuse
        # this registry, so facts and already-processed URIs carry across jobs.
        blackboard = Blackboard()
        run_id = self._change_ledger.begin_round()
        logger.info("[Scheduler] Memory Change run: %s", run_id)
        tools = self._build_tools(blackboard)
        results: List[JobResult] = []

        for job_class in self._job_classes:
            if self._shutdown:
                logger.info("Shutdown requested, skipping remaining jobs")
                break

            job = job_class(team_name=self._config.team_name)
            change_cursor = self._change_ledger.begin_job(job.name)
            logger.info("-" * 40)
            logger.info("[Scheduler] Running job: %s (priority=%d)", job.name, job.priority)

            # Create a fresh engine for each job with the job's specific system prompt
            job_config = self._job_config(job.name)
            engine = ReActEngine(
                config=job_config,
                tools=tools,
                system_prompt=(
                    self._config.job_prompts.get(job.name)
                    or job.get_system_prompt()
                ),
            )

            try:
                result = job.execute(engine)
                result.memory_changes = self._change_ledger.summaries_since(
                    change_cursor
                )
                results.append(result)
                logger.info(
                    "[Scheduler] Job %s: %s (%.1fs, %d tasks done)",
                    job.name, result.status.value,
                    result.duration_seconds, result.tasks_completed,
                )
            except Exception as e:
                logger.error("[Scheduler] Job %s crashed: %s", job.name, e, exc_info=True)
                failed_result = JobResult(
                    job_name=job.name,
                    status=JobStatus.FAILED,
                    started_at=datetime.now(timezone.utc),
                    errors=[str(e)],
                    summary=f"Crashed: {e}",
                )
                failed_result.memory_changes = (
                    self._change_ledger.summaries_since(change_cursor)
                )
                results.append(failed_result)

        # Record state
        self._state.record_round(results)
        self._log_round_summary(results)
        return results

    def run_once(self) -> List[JobResult]:
        """Run a single round immediately, ignoring time window."""
        logger.info("DreamCycle: running single round (--once mode)")
        return self.run_round()

    def status(self) -> Dict[str, Any]:
        """Return scheduler state for the embedded control plane."""
        return {
            "active_window": self.is_active_window(),
            "last_run_date": self._state.last_run_date,
            "rounds_today": self._state.rounds_today,
            "total_cycles": self._state.total_cycles,
            "history": self._state.history,
        }

    def shutdown(self) -> None:
        """Request a graceful stop without sending process-level signals."""
        self._shutdown = True

    def run_daemon(self) -> None:
        """Run continuously, executing rounds during active window."""
        logger.info(
            "DreamCycle daemon starting (window: %02d:00-%02d:00, max %d rounds/night, interval %dm)",
            self._config.scheduler.active_start_hour,
            self._config.scheduler.active_end_hour,
            self._config.scheduler.rounds_per_window,
            self._config.scheduler.round_interval_minutes,
        )

        while not self._shutdown:
            if self.is_active_window():
                if self._state.should_run_round(self._config.scheduler.rounds_per_window):
                    try:
                        self.run_round()
                    except Exception as e:
                        logger.error("Round execution failed: %s", e, exc_info=True)
                        time.sleep(self._config.scheduler.retry_delay_seconds)
                        continue

                    # Wait between rounds
                    wait = self._config.scheduler.round_interval_minutes * 60
                    logger.info("Round complete. Waiting %d minutes before next round...", wait // 60)
                    self._interruptible_sleep(wait)
                else:
                    logger.debug("All rounds for today completed. Waiting for next day.")
                    self._interruptible_sleep(1800)  # Check every 30 min
            else:
                # Outside active window — sleep and check periodically
                next_start = self._config.scheduler.active_start_hour
                now_hour = datetime.now().hour
                hours_until = (next_start - now_hour) % 24
                logger.debug("Outside active window. Next window in ~%dh. Sleeping.", hours_until)
                self._interruptible_sleep(min(hours_until * 3600, 3600))  # At most 1h sleep

        logger.info("DreamCycle daemon shutting down gracefully")

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that can be interrupted by shutdown signal."""
        end = time.time() + seconds
        while time.time() < end and not self._shutdown:
            time.sleep(min(10, end - time.time()))

    def _log_round_summary(self, results: List[JobResult]) -> None:
        """Log a summary of the round."""
        total = len(results)
        completed = sum(1 for r in results if r.status == JobStatus.COMPLETED)
        failed = sum(1 for r in results if r.status == JobStatus.FAILED)
        total_duration = sum(r.duration_seconds for r in results)
        total_turns = sum(r.turns_used for r in results)
        total_actions = sum(r.actions_taken for r in results)

        logger.info("=" * 60)
        logger.info("ROUND SUMMARY")
        logger.info("  Jobs: %d total, %d completed, %d failed", total, completed, failed)
        logger.info("  Duration: %.1fs total", total_duration)
        logger.info("  LLM turns: %d total", total_turns)
        logger.info("  Actions: %d total", total_actions)
        logger.info("  State: cycle #%d, round %d today", self._state.total_cycles, self._state.rounds_today)
        logger.info("=" * 60)

        # Also log individual job results
        for r in results:
            icon = "✅" if r.status == JobStatus.COMPLETED else "❌"
            logger.info("  %s %s: %s (%.1fs, %d turns)", icon, r.job_name, r.status.value, r.duration_seconds, r.turns_used)
