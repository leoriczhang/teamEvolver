"""Platform OpenViking Compile client.

Wraps the generic :class:`OpenVikingClient` with the Compile task protocol:
submit / status / list / cancel / capabilities / get_skill. Each submit carries
a unified ``Idempotency-Key`` and results are normalized to the terminal-state
enum in :mod:`teamEvolver.openviking.schemas`.

Memory/Wiki URI rules and success post-processing are NOT here — the
business modules own those.
"""

from __future__ import annotations

import re
from typing import Any

from .client import OpenVikingClient, OpenVikingError, OpenVikingResult
from .schemas import CompileTask, normalize_state


class CompileClient:
    def __init__(self, client: OpenVikingClient) -> None:
        self._client = client

    @staticmethod
    def _as_task(result: OpenVikingResult, *, task_id: str = "") -> CompileTask:
        data = result.data if isinstance(result.data, dict) else {}
        tid = str(data.get("task_id") or data.get("id") or task_id)
        if not result.ok:
            # An unavailable status endpoint says nothing about the task's
            # outcome. Callers must reconcile, never finalize it as failed.
            raise OpenVikingError(f"OpenViking HTTP {result.status_code}", status_code=result.status_code)
        raw = data.get("status") or data.get("state") or ""
        return CompileTask(
            task_id=tid,
            state=normalize_state(raw),
            raw_status=str(raw),
            result=data,
        )

    async def submit(
        self, *, kind: str, payload: dict[str, Any], idempotency_key: str
    ) -> CompileTask:
        """Submit a Compile task; idempotency key dedupes retries server-side."""
        body = dict(payload)
        result = await self._client.request(
            "POST", "/api/v1/compile", json_body=body, idempotency_key=idempotency_key
        )
        task = self._as_task(result)
        if not task.task_id:
            raise OpenVikingError("Compile submission outcome unknown; reconcile by key", status_code=502)
        return task

    @staticmethod
    def _task_id(task_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
            raise ValueError("invalid OpenViking task ID")
        return task_id

    async def status(self, task_id: str) -> CompileTask:
        task_id = self._task_id(task_id)
        result = await self._client.request("GET", f"/api/v1/tasks/{task_id}")
        return self._as_task(result, task_id=task_id)

    async def submission(self, idempotency_key: str) -> CompileTask:
        if not re.fullmatch(r"[A-Za-z0-9:._-]{16,128}", idempotency_key):
            raise ValueError("invalid Compile idempotency key")
        result = await self._client.request("GET", f"/api/v1/compile/submissions/{idempotency_key}")
        return self._as_task(result)

    async def list_tasks(self, *, params: dict[str, Any] | None = None) -> OpenVikingResult:
        return await self._client.request("GET", "/api/v1/tasks", params=params or {})

    async def cancel(self, task_id: str) -> CompileTask:
        task_id = self._task_id(task_id)
        result = await self._client.request("POST", f"/api/v1/tasks/{task_id}/cancel")
        task = self._as_task(result, task_id=task_id)
        # Cancellation can race completion or merely acknowledge a request.
        # Preserve the upstream state until status confirms a terminal result.
        return task

    async def capabilities(self) -> OpenVikingResult:
        return await self._client.request("GET", "/api/v1/compile/capabilities")

    async def get_skill(self, *, skill_name: str, params=None, shared_admin=False) -> OpenVikingResult:
        return await self._client.request("GET", f"/api/v1/skills/{skill_name}",
                                          params=params, shared_admin=shared_admin)
