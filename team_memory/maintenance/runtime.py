"""Legacy DreamCycle entry points backed by the unified compile workflow.

Historical ledgers and Memory Replay remain readable through the old Interface.
No legacy ReAct Job is selected or executed.
"""

from team_memory.maintenance.integrations.dreamcycle_runtime import FullDreamCycleSupervisor
from team_memory.service import MemoryAggregationService


class TeamMemorySupervisor(FullDreamCycleSupervisor):
    def __init__(self, config, service=None):
        self._team_memory = service or MemoryAggregationService(config)
        super().__init__(config)

    def _selected_job_classes(self):
        return []

    def _missing(self):
        values = {
            "sharing.viking_endpoint": self.config.sharing_viking_endpoint,
            "sharing.viking_api_key": self.config.sharing_viking_team_api_key or self.config.sharing_viking_api_key,
        }
        return [key for key, value in values.items() if not str(value or "").strip()]

    def _run_round(self):
        if not self._round_lock.acquire(blocking=False):
            return
        run = None
        try:
            import asyncio

            run = self._team_memory.new_run(self.config.sharing_viking_account or "default", pipeline="maintain")
            key = self.config.sharing_viking_team_api_key or self.config.sharing_viking_api_key
            asyncio.run(self._team_memory.reconcile(run, key))
            self._team_memory.submit(run, api_key=key)
            while run.status in {"pending", "running"}:
                if self._stop_event.wait(0.5):
                    return
            self._last_results = [run.to_public()]
            self._last_error = run.error
            self._scheduler._state.record_round([])
        except Exception as exc:
            self._last_error = str(exc)
            if run and run.status == "pending":
                self._team_memory.forget_run(run.task_id)
        finally:
            self._round_lock.release()

    def dry_run(self):
        return {
            "engine": "team-memory-compile",
            "maintained_space": self._team_memory.resolve_target_uri(),
            "jobs": [],
            "stages": ["snapshot", "maintenance"],
            "skill_uri": self._team_memory._shared_skill_uri("maintenance"),
        }

    def status(self):
        return {
            **super().status(),
            "engine": "team-memory-compile",
            "full_capabilities": False,
            "maintained_space": self._team_memory.resolve_target_uri(),
            "jobs": [],
            "tools": ["ov_compile", "ov_cp"],
            "runs": self._team_memory.list_runs(),
        }

    def reset(self, *, remote=False, dry_run=True):
        if remote:
            return {"status": "unsupported", "error": "Remote Memory reset is disabled; snapshots are retained"}
        return super().reset(remote=False, dry_run=dry_run)
