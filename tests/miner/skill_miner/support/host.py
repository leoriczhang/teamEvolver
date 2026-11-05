"""Deterministic host adapter for domain regressions; production uses the PG engine."""
from __future__ import annotations

import asyncio

import httpx
from starlette.exceptions import HTTPException

from team_miner.skill_miner.engine_polling import MiningPollingMixin
from team_miner.skill_miner.engine_resources import MiningResourceMixin
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin

from . import admission as compile_admission


class AdmissionMixin:
    async def _acquire_staged_compile_slot(self, job: dict, state: dict, current: dict) -> bool:
        if state.get("execution_protocol") != compile_admission.OPTIMIZED_PROTOCOL:
            return True
        repository = self._ov_skill_mining_repository()
        if await asyncio.to_thread(compile_admission.acquire, repository, job, current):
            return True
        # A terminal task may have been observed just before the process stopped.
        # Check only the two durable reservations; never expire a live task by age.
        for row in await asyncio.to_thread(compile_admission.reservations, repository.jobs_root):
            if not row.get("task_id"):
                try:
                    accepted = await self._knowledge_request(
                        row["account"], "GET", f"/api/v1/compile/submissions/{row['key']}", timeout=5.0
                    )
                except (HTTPException, httpx.HTTPError, OSError):
                    continue
                if not isinstance(accepted, dict) or not accepted.get("task_id"):
                    continue
                row["task_id"] = str(accepted["task_id"])
                await asyncio.to_thread(
                    compile_admission.accepted, repository.jobs_root, row["key"], row["task_id"]
                )
            try:
                task = await self._knowledge_request(
                    row["account"], "GET", f"/api/v1/tasks/{row['task_id']}", timeout=5.0
                )
            except (HTTPException, httpx.HTTPError, OSError):
                continue
            if isinstance(task, dict) and task.get("status") in compile_admission.TERMINAL_NATIVE_STATES:
                await asyncio.to_thread(compile_admission.release, repository.jobs_root, task_id=row["task_id"])
        return await asyncio.to_thread(compile_admission.acquire, repository, job, current)


    async def _mark_staged_compile_slot(self, job: dict, current: dict) -> None:
        await asyncio.to_thread(
            compile_admission.accepted, self._ov_skill_mining_repository().jobs_root,
            current["key"], current["task_id"],
        )


    async def _release_staged_compile_slot(self, job: dict, task_id: str = "", *, key: str = "") -> None:
        await asyncio.to_thread(
            compile_admission.release, self._ov_skill_mining_repository().jobs_root, task_id=task_id, key=key
        )



class DomainHost(AdmissionMixin, MiningPollingMixin, MiningResourceMixin, OvRoundOrchestrationMixin):
    def _ov_skill_mining_lock(self, name):
        if not hasattr(self, "_domain_locks"):
            self._domain_locks = {}
        return self._domain_locks.setdefault(name, asyncio.Lock())
