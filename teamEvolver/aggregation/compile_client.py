"""HTTP adapter for OpenViking Skill installation and compile tasks.

The aggregation worker talks to OpenViking's public HTTP interface directly:

- ``POST /api/v1/skills`` installs the editable Skill from inline content.
- ``POST /bot/v1/compile`` creates a compile task.
- ``GET /bot/v1/compile/{task_id}`` polls it to a terminal state.

No local ``ov`` binary or shared filesystem is required. This matters when
OpenViking runs in a separate container and its CLI is not installed on the
teamEvolver host.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

_MAX_HTTP_OUTPUT_CHARS = 512 * 1024


@dataclass
class CompileClient:
    """Run OpenViking compile operations with a request-scoped Admin Key."""

    endpoint: str
    account_id: str
    user_id: str = ""
    api_key: str = ""
    agent_id: str = "team-skill-evolver"
    timeout_seconds: float = 3000.0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "X-OpenViking-Account": self.account_id,
            "X-OpenViking-Actor-Peer": self.agent_id,
        }
        if self.user_id.strip():
            headers["X-OpenViking-User"] = self.user_id.strip()
        return headers

    @staticmethod
    def _payload(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return {}
        if (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and "result" in payload
        ):
            return payload["result"]
        return payload

    @staticmethod
    def _error_message(response: httpx.Response, payload: Any) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                if message:
                    return message[:_MAX_HTTP_OUTPUT_CHARS]
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if detail:
                return str(detail)[:_MAX_HTTP_OUTPUT_CHARS]
        return str(response.text or f"HTTP {response.status_code}")[
            :_MAX_HTTP_OUTPUT_CHARS
        ]

    @staticmethod
    def _success(operation: str, payload: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "exit_code": 0,
            "command": ["http", operation],
            "stdout": json.dumps(payload, ensure_ascii=False),
            "stderr": "",
            "result": payload,
        }

    @classmethod
    def _failure(
        cls,
        operation: str,
        response: httpx.Response,
        payload: Any,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "exit_code": response.status_code,
            "command": ["http", operation],
            "stdout": "",
            "stderr": cls._error_message(response, payload),
            "result": payload,
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=max(30.0, self.timeout_seconds + 30.0),
            follow_redirects=False,
        )

    async def install_skill(
        self,
        *,
        skill_name: str,
        skill_body: str,
        parent_uri: str = "viking://agent/skills",
    ) -> dict[str, Any]:
        """Install inline Skill content without exposing a host-local path."""
        operation = "POST /api/v1/skills"
        body = {
            "data": skill_body,
            "wait": True,
            "timeout": self.timeout_seconds,
            "target_uri": parent_uri,
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.endpoint.rstrip('/')}/api/v1/skills",
                    json=body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "command": ["http", operation, skill_name],
                "stdout": "",
                "stderr": f"OpenViking Skill upload failed: {exc}",
                "result": None,
            }
        payload = self._payload(response)
        if not response.is_success:
            return self._failure(operation, response, payload)
        return self._success(operation, payload)

    async def run_batch(
        self,
        *,
        source_uris: tuple[str, ...] | list[str],
        target_uri: str,
        skill_uri: str,
        reason: str = "",
        runtime_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Create and poll one OpenViking compile task over HTTP."""
        if not source_uris:
            return {"ok": True, "skipped": True, "reason": "no sources"}

        operation = "POST /bot/v1/compile"
        body: dict[str, Any] = {
            "from": list(source_uris),
            "to": target_uri,
            "skill": skill_uri,
        }
        if reason.strip():
            body["reason"] = reason.strip()
        if runtime_timeout_seconds:
            body["runtime_timeout_seconds"] = float(runtime_timeout_seconds)

        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.endpoint.rstrip('/')}/bot/v1/compile",
                    json=body,
                    headers=self._headers(),
                )
                accepted = self._payload(response)
                if not response.is_success:
                    return self._failure(operation, response, accepted)
                task_id = (
                    str(accepted.get("task_id") or "").strip()
                    if isinstance(accepted, dict)
                    else ""
                )
                if not task_id:
                    return {
                        "ok": False,
                        "exit_code": -1,
                        "command": ["http", operation],
                        "stdout": json.dumps(accepted, ensure_ascii=False),
                        "stderr": "OpenViking compile response did not include task_id",
                        "result": accepted,
                    }

                deadline = time.monotonic() + max(1.0, self.timeout_seconds)
                polling = 0.5
                status_operation = f"GET /bot/v1/compile/{task_id}"
                while True:
                    if time.monotonic() >= deadline:
                        return {
                            "ok": False,
                            "exit_code": -1,
                            "command": ["http", status_operation],
                            "stdout": "",
                            "stderr": (
                                f"OpenViking compile timed out after "
                                f"{self.timeout_seconds}s; task_id={task_id}"
                            ),
                            "result": accepted,
                        }
                    status_response = await client.get(
                        f"{self.endpoint.rstrip('/')}/bot/v1/compile/{task_id}",
                        headers=self._headers(),
                    )
                    task = self._payload(status_response)
                    if not status_response.is_success:
                        return self._failure(
                            status_operation,
                            status_response,
                            task,
                        )
                    status = (
                        str(task.get("status") or "").lower()
                        if isinstance(task, dict)
                        else ""
                    )
                    if status == "completed":
                        result = task.get("result")
                        return self._success(
                            status_operation,
                            result if result is not None else task,
                        )
                    if status in {"failed", "cancelled"}:
                        error = task.get("error") if isinstance(task, dict) else None
                        if isinstance(error, dict):
                            detail = str(
                                error.get("message") or error.get("code") or status
                            )
                        else:
                            detail = str(error or f"compile task {status}")
                        return {
                            "ok": False,
                            "exit_code": 1,
                            "command": ["http", status_operation],
                            "stdout": json.dumps(task, ensure_ascii=False),
                            "stderr": detail[:_MAX_HTTP_OUTPUT_CHARS],
                            "result": task,
                        }
                    await asyncio.sleep(polling)
                    polling = min(2.0, polling * 2)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "command": ["http", operation],
                "stdout": "",
                "stderr": f"OpenViking compile request failed: {exc}",
                "result": None,
            }
