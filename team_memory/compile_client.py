"""HTTP adapter for OpenViking Skill installation and compile tasks.

The aggregation worker talks to OpenViking's public HTTP interface directly:

- ``POST /api/v1/skills`` installs the editable Skill from inline content.
- ``POST /api/v1/compile`` creates an OpenViking-owned task.
- ``GET /api/v1/tasks/{task_id}`` polls it to a terminal state.

No local ``ov`` binary or shared filesystem is required. This matters when
OpenViking runs in a separate container and its CLI is not installed on the
teamEvolver host.

Incremental compile: ``run_batch`` accepts an optional ``last_compile_time``.
When supplied it is forwarded to OpenViking as ``args.last_compile_time`` (the
HTTP equivalent of ``ov compile --args '{"last_compile_time": ...}'``).
OpenViking then recursively lists the ``from`` sources, de-duplicates files
across sources, and skips any file whose ``modTime`` (falling back to
``mtime``) is strictly earlier than that instant; files whose update time is
missing or unparseable are kept to avoid dropping edits. When every file is
filtered out the task still finishes successfully without invoking the model
or writing artifacts. Omitting it keeps the full compile. The value is caller
supplied; teamEvolver never records or advances it automatically.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

_MAX_HTTP_OUTPUT_CHARS = 512 * 1024
_HTTP_RETRY_ATTEMPTS = 3
_HTTP_RETRY_BASE_SECONDS = 0.25


def normalize_last_compile_time(value: Any) -> str:
    """Normalize a caller ``last_compile_time`` into an ISO-8601 UTC instant.

    OpenViking accepts an ISO time string or a Unix timestamp (seconds). We
    validate and canonicalize on the client so an obviously malformed value is
    rejected before a task is submitted, mirroring OpenViking's own contract:
    a naive datetime is interpreted as UTC and an unparseable value is an
    error. The return value is a timezone-aware ISO-8601 string (``+00:00``).
    """
    if value is None:
        raise ValueError("last_compile_time must not be null")
    # Unix timestamp (seconds), accepted as int/float or a bare numeric string.
    if isinstance(value, bool):
        raise ValueError("last_compile_time must be a timestamp or ISO string")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"invalid last_compile_time timestamp: {value!r}") from exc
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("last_compile_time must not be empty")
        # A bare numeric string is treated as a Unix timestamp too.
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
        except ValueError:
            pass
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise ValueError(f"invalid last_compile_time: {value!r}") from exc
        if parsed.tzinfo is None:
            # No timezone in the input: interpret as UTC, matching OV.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    raise ValueError("last_compile_time must be a timestamp or ISO string")


@dataclass
class CompileClient:
    """Run OpenViking compile operations with a request-scoped API key."""

    endpoint: str
    account_id: str
    user_id: str = ""
    api_key: str = ""
    agent_id: str = "team-skill-evolver"
    timeout_seconds: float = 3000.0
    task_callback: Callable[[str, str], None] | None = None

    def _headers(self, *, shared_skill: bool = False) -> dict[str, str]:
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
        if shared_skill:
            # Shared Skill publication needs the trusted admin assertion and
            # must not be narrowed to one actor-peer view.
            headers["X-OpenViking-Role"] = "admin"
            headers.pop("X-OpenViking-Actor-Peer", None)
        return headers

    @staticmethod
    def _payload(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return {}
        if isinstance(payload, dict) and payload.get("status") == "ok" and "result" in payload:
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
        return str(response.text or f"HTTP {response.status_code}")[:_MAX_HTTP_OUTPUT_CHARS]

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

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        retry_all_transport_errors: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        retryable: tuple[type[httpx.TransportError], ...] = (
            (httpx.TransportError,) if retry_all_transport_errors else (httpx.ConnectError, httpx.ConnectTimeout)
        )
        for attempt in range(1, _HTTP_RETRY_ATTEMPTS + 1):
            try:
                request = getattr(client, method.lower())
                return await request(url, **kwargs)
            except retryable:
                if attempt >= _HTTP_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_HTTP_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
        raise RuntimeError("unreachable")

    async def install_skill(
        self,
        *,
        skill_name: str,
        skill_body: str,
        parent_uri: str = "viking://agent/skills",
        version_message: str = "",
    ) -> dict[str, Any]:
        """Install inline Skill content without exposing a host-local path."""
        operation = "POST /api/v1/skills"
        # Keep the body within OpenViking's accepted fields. A version note is
        # carried inside source_metadata (an arbitrary dict) rather than as a
        # top-level version_message, which OV's schema rejects.
        body: dict[str, Any] = {
            "data": skill_body,
            "wait": True,
            "timeout": self.timeout_seconds,
            "target_uri": parent_uri,
        }
        if version_message.strip():
            body["source_metadata"] = {
                "type": "api",
                "source": "inline_content",
                "operation": "install",
                "version_message": version_message.strip(),
            }
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "POST",
                    f"{self.endpoint.rstrip('/')}/api/v1/skills",
                    json=body,
                    headers=self._headers(shared_skill=parent_uri.rstrip("/") == "viking://agent/skills"),
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

    async def get_skill(self, *, skill_name: str) -> dict[str, Any]:
        """Read the account-shared Skill with a content-addressed revision."""
        operation = f"GET /api/v1/skills/{skill_name}"
        expected_root_uri = f"viking://agent/skills/{skill_name}".rstrip("/")
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "GET",
                    f"{self.endpoint.rstrip('/')}/api/v1/skills/{skill_name}",
                    params={
                        "target_uri": "viking://agent/skills",
                        "include_content": "true",
                        "include_files": "true",
                        "include_integrity": "true",
                    },
                    headers=self._headers(shared_skill=True),
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "command": ["http", operation],
                "stdout": "",
                "stderr": f"OpenViking Skill read failed: {exc}",
                "result": None,
            }
        payload = self._payload(response)
        if not response.is_success:
            return self._failure(operation, response, payload)
        actual_root_uri = str(payload.get("root_uri") or "").rstrip("/") if isinstance(payload, dict) else ""
        if actual_root_uri != expected_root_uri:
            return {
                "ok": False,
                "exit_code": 404,
                "command": ["http", operation],
                "stdout": "",
                "stderr": f"Shared Skill not found at {expected_root_uri}",
                "result": payload,
            }
        return self._success(operation, payload)

    async def publish_shared_skill(
        self,
        *,
        skill_name: str,
        skill_body: str,
        version_message: str,
    ) -> dict[str, Any]:
        """Create or replace one shared Skill, then return its exact revision."""
        current = await self.get_skill(skill_name=skill_name)
        if current.get("ok"):
            detail = current.get("result") or {}
            if str(detail.get("content") or "") != skill_body:
                operation = f"PUT /api/v1/skills/{skill_name}"
                # OpenViking's update endpoint forbids unknown body fields, so
                # only send what UpdateSkillRequest accepts. version_message is
                # carried inside source_metadata (an arbitrary dict OV accepts)
                # rather than as a rejected top-level field; OV owns revisioning
                # so no client-side expected_revision is sent.
                body = {
                    "data": skill_body,
                    "wait": True,
                    "timeout": self.timeout_seconds,
                    "target_uri": "viking://agent/skills",
                    "source_metadata": {
                        "type": "api",
                        "source": "inline_content",
                        "operation": "update",
                        "version_message": version_message,
                    },
                }
                try:
                    async with self._client() as client:
                        response = await self._request_with_retry(
                            client,
                            "PUT",
                            f"{self.endpoint.rstrip('/')}/api/v1/skills/{skill_name}",
                            json=body,
                            headers=self._headers(shared_skill=True),
                        )
                except httpx.HTTPError as exc:
                    return {
                        "ok": False,
                        "exit_code": -1,
                        "command": ["http", operation],
                        "stdout": "",
                        "stderr": f"OpenViking shared Skill update failed: {exc}",
                        "result": None,
                    }
                payload = self._payload(response)
                if not response.is_success:
                    return self._failure(operation, response, payload)
        elif int(current.get("exit_code") or 0) == 404:
            created = await self.install_skill(
                skill_name=skill_name,
                skill_body=skill_body,
                parent_uri="viking://agent/skills",
                version_message=version_message,
            )
            if not created.get("ok"):
                return created
        else:
            return current
        return await self.get_skill(skill_name=skill_name)

    async def delete_uri(self, *, uri: str) -> dict[str, Any]:
        """Delete an obsolete resource subtree."""
        operation = "DELETE /api/v1/fs"
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "DELETE",
                    f"{self.endpoint.rstrip('/')}/api/v1/fs",
                    params={
                        "uri": uri,
                        "recursive": "true",
                        "wait": "false",
                    },
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "command": ["http", operation],
                "stdout": "",
                "stderr": f"OpenViking content delete failed: {exc}",
                "result": None,
            }
        payload = self._payload(response)
        if response.status_code == 404:
            return self._success(operation, {"uri": uri, "missing": True})
        if not response.is_success:
            return self._failure(operation, response, payload)
        return self._success(operation, payload)

    async def copy_tree(self, *, source_uri: str, target_uri: str) -> dict[str, Any]:
        """Server-side ov cp -r. Callers supply a fresh, private destination."""
        operation = "POST /api/v1/fs/cp"
        async with self._client() as client:
            response = await self._request_with_retry(
                client,
                "POST",
                f"{self.endpoint.rstrip('/')}/api/v1/fs/cp",
                json={"from_uri": source_uri, "to_uri": target_uri, "recursive": True},
                headers=self._headers(),
            )
        payload = self._payload(response)
        if not response.is_success:
            return self._failure(operation, response, payload)
        return self._success(operation, payload)

    async def mkdir(self, uri: str) -> None:
        async with self._client() as client:
            response = await self._request_with_retry(
                client,
                "POST",
                f"{self.endpoint.rstrip('/')}/api/v1/fs/mkdir",
                json={"uri": uri},
                headers=self._headers(),
            )
        if not response.is_success and response.status_code != 409:
            raise ValueError(self._error_message(response, self._payload(response)))

    async def read_text(self, uri: str) -> str:
        async with self._client() as client:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.endpoint.rstrip('/')}/api/v1/content/read",
                params={"uri": uri},
                headers=self._headers(),
            )
        payload = self._payload(response)
        if not response.is_success or not isinstance(payload, str):
            raise ValueError(f"Cannot read {uri}: {self._error_message(response, payload)}")
        return payload

    async def task_status(self, task_id: str) -> dict[str, Any]:
        if not task_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Invalid OpenViking task ID")
        async with self._client() as client:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.endpoint.rstrip('/')}/api/v1/tasks/{task_id}",
                headers=self._headers(),
                retry_all_transport_errors=True,
            )
        payload = self._payload(response)
        if not response.is_success or not isinstance(payload, dict):
            raise ValueError(self._error_message(response, payload))
        return payload

    async def download_bytes(self, uri: str) -> bytes:
        async with self._client() as client:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.endpoint.rstrip('/')}/api/v1/content/download",
                params={"uri": uri},
                headers=self._headers(),
            )
        if not response.is_success:
            raise ValueError(f"Cannot download Skill file: {uri}")
        return response.content

    async def run_batch(
        self,
        *,
        source_uris: tuple[str, ...] | list[str],
        target_uri: str,
        skill_uri: str,
        skill_revision: str = "",
        reason: str = "",
        runtime_timeout_seconds: float | None = None,
        last_compile_time: Any = None,
    ) -> dict[str, Any]:
        """Create and poll one OpenViking compile task over HTTP.

        When ``last_compile_time`` is supplied it is forwarded as
        ``args.last_compile_time`` so OpenViking only recompiles sources modified
        at or after that instant (incremental compile). A malformed value is
        rejected here, before a task is created upstream.
        """
        if not source_uris:
            return {"ok": True, "skipped": True, "reason": "no sources"}

        operation = "POST /api/v1/compile"
        body: dict[str, Any] = {
            "from": list(source_uris),
            "to": target_uri,
            "skill": skill_uri,
        }
        # Revision is local provenance only; the new strict schema rejects it.
        # The orchestrator supplies a run-private Skill URI to freeze execution.
        if reason.strip():
            body["instruction"] = reason.strip()
        if last_compile_time is not None:
            try:
                normalized = normalize_last_compile_time(last_compile_time)
            except ValueError as exc:
                return {
                    "ok": False,
                    "exit_code": -1,
                    "command": ["http", operation],
                    "stdout": "",
                    "stderr": str(exc),
                    "result": None,
                }
            # ov compile --args '{"last_compile_time": ...}'. Only files at or
            # after this instant are recompiled; the rest are skipped upstream.
            body["args"] = {"last_compile_time": normalized}

        task_id = ""
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "POST",
                    f"{self.endpoint.rstrip('/')}/api/v1/compile",
                    json=body,
                    headers=self._headers(),
                )
                accepted = self._payload(response)
                if not response.is_success:
                    return self._failure(operation, response, accepted)
                task_id = str(accepted.get("task_id") or "").strip() if isinstance(accepted, dict) else ""
                if not task_id:
                    if self.task_callback:
                        self.task_callback("submission_unknown", "unknown")
                    return {
                        "ok": False,
                        "exit_code": -1,
                        "command": ["http", operation],
                        "stdout": json.dumps(accepted, ensure_ascii=False),
                        "stderr": "OpenViking compile response did not include task_id",
                        "result": accepted,
                    }
                if self.task_callback:
                    self.task_callback(task_id, "pending")

                deadline = time.monotonic() + max(1.0, self.timeout_seconds)
                polling = 0.5
                status_operation = f"GET /api/v1/tasks/{task_id}"
                while True:
                    if time.monotonic() >= deadline:
                        return {
                            "ok": False,
                            "exit_code": -1,
                            "command": ["http", status_operation],
                            "stdout": "",
                            "stderr": (
                                f"OpenViking compile timed out after {self.timeout_seconds}s; task_id={task_id}"
                            ),
                            "result": accepted,
                        }
                    status_response = await self._request_with_retry(
                        client,
                        "GET",
                        f"{self.endpoint.rstrip('/')}/api/v1/tasks/{task_id}",
                        headers=self._headers(),
                        retry_all_transport_errors=True,
                    )
                    task = self._payload(status_response)
                    if not status_response.is_success:
                        return self._failure(
                            status_operation,
                            status_response,
                            task,
                        )
                    status = str(task.get("status") or "").lower() if isinstance(task, dict) else ""
                    if status == "completed":
                        if self.task_callback:
                            self.task_callback(task_id, status)
                        result = task.get("result")
                        return self._success(
                            status_operation,
                            result if result is not None else task,
                        )
                    if status in {"failed", "cancelled"}:
                        if self.task_callback:
                            self.task_callback(task_id, status)
                        error = task.get("error") if isinstance(task, dict) else None
                        if isinstance(error, dict):
                            detail = str(error.get("message") or error.get("code") or status)
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
            if not task_id and self.task_callback and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                self.task_callback("submission_unknown", "unknown")
            return {
                "ok": False,
                "exit_code": -1,
                "command": [
                    "http",
                    f"GET /api/v1/tasks/{task_id}" if task_id else operation,
                ],
                "stdout": "",
                "stderr": (
                    "OpenViking compile status request failed" if task_id else "OpenViking compile request failed"
                )
                + f": {exc}",
                "result": None,
            }
