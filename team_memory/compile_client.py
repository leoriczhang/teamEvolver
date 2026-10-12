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
import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

import httpx
import yaml

_MAX_HTTP_OUTPUT_CHARS = 512 * 1024
_HTTP_RETRY_ATTEMPTS = 3
_HTTP_RETRY_BASE_SECONDS = 0.25


def _skill_content_signature(content: str) -> tuple[Any, ...]:
    """Compare the fields preserved by OV SkillLoader, allowing YAML normalization."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not match:
        raise ValueError("Skill package requires SKILL.md YAML frontmatter")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError("Invalid SKILL.md YAML frontmatter") from exc
    if not isinstance(metadata, dict) or not all(
        isinstance(metadata.get(key), str) and metadata[key].strip() for key in ("name", "description")
    ):
        raise ValueError("SKILL.md requires a name and description")
    tools = metadata.get("allowed-tools", [])
    if isinstance(tools, str):
        parts, current, depth = [], [], 0
        for char in tools.strip():
            depth += (char == "(") - (char == ")")
            if depth < 0:
                raise ValueError("Invalid SKILL.md allowed-tools parentheses")
            if depth == 0 and (char.isspace() or char == ","):
                if current:
                    parts.append("".join(current))
                    current = []
            else:
                current.append(char)
        if depth:
            raise ValueError("Invalid SKILL.md allowed-tools parentheses")
        tools = [*parts, *(["".join(current)] if current else [])]
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        raise ValueError("Invalid SKILL.md allowed-tools")
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        tags = list(tags) if isinstance(tags, (tuple, set)) else [tags]
    return (
        metadata["name"], metadata["description"], tools, "allowed-tools" in metadata, tags,
        metadata.get("metadata"), match[2].strip(),
    )


def _skill_package_files(skill_name: str, skill_body: str, auxiliary_files: Mapping[str, str]) -> dict[str, bytes]:
    """Build only safe, bounded paths for OV's ZIP package and integrity limits."""
    if not isinstance(skill_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", skill_name):
        raise ValueError("Invalid Skill package name or body")
    if not isinstance(skill_body, str):
        raise ValueError("Invalid Skill package name or body")
    if _skill_content_signature(skill_body)[0] != skill_name:
        raise ValueError("SKILL.md name must match the published Skill name")
    if not isinstance(auxiliary_files, Mapping):
        raise ValueError("auxiliary_files must map relative paths to UTF-8 text")
    files = {"SKILL.md": skill_body.encode("utf-8")}
    reserved = {".abstract.md", ".overview.md", ".source.json"}
    for path, content in auxiliary_files.items():
        if (
            not isinstance(path, str) or not path or not isinstance(content, str)
            or any(char in path for char in "\\\x00%?#:") or any(ord(char) < 32 for char in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or len(path.split("/")) > 10 or path == "SKILL.md" or PurePosixPath(path).is_absolute()
            or PurePosixPath(path).name in reserved
        ):
            raise ValueError("Invalid Skill auxiliary file path or content")
        files[path] = content.encode("utf-8")
    directories = {str(parent) for path in files for parent in PurePosixPath(path).parents if str(parent) != "."}
    if directories.intersection(files):
        raise ValueError("Skill package file and directory paths conflict")
    # GET include_integrity uses 512 entries, 16 MiB per file and 64 MiB total.
    if len(files) + len(directories) + 2 > 512 or any(len(data) > 16 * 1024 * 1024 for data in files.values()):
        raise ValueError("Skill package exceeds OpenViking integrity limits")
    if sum(map(len, files.values())) > 64 * 1024 * 1024:
        raise ValueError("Skill package exceeds OpenViking integrity limits")
    return files


def _skill_package_matches(detail: Any, skill_body: str, files: dict[str, bytes]) -> bool:
    """Verify a locked OV manifest: file paths and raw UTF-8 SHA-256 values."""
    if not isinstance(detail, dict) or not isinstance(detail.get("content"), str):
        return False
    try:
        if _skill_content_signature(detail["content"]) != _skill_content_signature(skill_body):
            return False
    except ValueError:
        return False
    entries = detail.get("files")
    if not isinstance(entries, list):
        return False
    actual = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if entry.get("is_dir") is True:
            continue
        path = entry.get("path")
        if not isinstance(path, str) or path in actual:
            return False
        if PurePosixPath(path).name in {".abstract.md", ".overview.md"}:
            continue
        actual[path] = entry.get("sha256")
    expected = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    # OV rewrites SKILL.md through SkillLoader; compare its semantic body above,
    # then bind the manifest digest to the exact returned canonical content.
    expected["SKILL.md"] = hashlib.sha256(detail["content"].encode("utf-8")).hexdigest()
    return actual == expected


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
    internal_port: Any = None
    compile_state_path: Any = None

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
        auxiliary_files: Mapping[str, str] | None = None,
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
        if auxiliary_files is not None:
            uploaded = await self._upload_skill_package(
                skill_name=skill_name, skill_body=skill_body, auxiliary_files=auxiliary_files,
                shared_skill=parent_uri.rstrip("/") == "viking://agent/skills",
            )
            if not uploaded.get("ok"):
                return uploaded
            body.pop("data")
            body["temp_file_id"] = uploaded["result"]["temp_file_id"]
        if version_message.strip():
            body["source_metadata"] = {
                "type": "api",
                "source": "temp_upload" if auxiliary_files is not None else "inline_content",
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

    async def _upload_skill_package(
        self, *, skill_name: str, skill_body: str, auxiliary_files: Mapping[str, str], shared_skill: bool,
    ) -> dict[str, Any]:
        operation = "POST /api/v1/resources/temp_upload"
        try:
            package = _skill_package_files(skill_name, skill_body, auxiliary_files)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, content in sorted(package.items()):
                    archive.writestr(path, content)
        except (ValueError, UnicodeError) as exc:
            return self._package_failure(operation, str(exc), 400)
        headers = self._headers(shared_skill=shared_skill)
        headers.pop("Content-Type", None)  # httpx owns the multipart boundary.
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client, "POST", f"{self.endpoint.rstrip('/')}/api/v1/resources/temp_upload",
                    files={"file": (f"{skill_name}.zip", stream.getvalue(), "application/zip")}, headers=headers,
                )
        except httpx.HTTPError:
            return self._package_failure(operation, "OpenViking Skill package upload failed", -1)
        payload = self._payload(response)
        if not response.is_success:
            return self._failure(operation, response, payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("temp_file_id"), str):
            return self._package_failure(operation, "OpenViking did not return a Skill package upload ID", 502)
        if not payload["temp_file_id"].strip():
            return self._package_failure(operation, "OpenViking did not return a Skill package upload ID", 502)
        return self._success(operation, payload)

    @staticmethod
    def _package_failure(operation: str, message: str, exit_code: int = 502) -> dict[str, Any]:
        return {
            "ok": False, "exit_code": exit_code, "command": ["http", operation],
            "stdout": "", "stderr": message, "result": None,
        }

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
        auxiliary_files: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create or replace one shared Skill, then return its exact revision."""
        package = None
        if auxiliary_files is not None:
            try:
                package = _skill_package_files(skill_name, skill_body, auxiliary_files)
            except (ValueError, UnicodeError) as exc:
                return self._package_failure("publish shared Skill", str(exc), 400)
        current = await self.get_skill(skill_name=skill_name)
        if current.get("ok"):
            detail = current.get("result") or {}
            unchanged = (
                _skill_package_matches(detail, skill_body, package) if package is not None
                else str(detail.get("content") or "") == skill_body
            )
            if not unchanged:
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
                if auxiliary_files is not None:
                    uploaded = await self._upload_skill_package(
                        skill_name=skill_name, skill_body=skill_body,
                        auxiliary_files=auxiliary_files, shared_skill=True,
                    )
                    if not uploaded.get("ok"):
                        return uploaded
                    body.pop("data")
                    body["temp_file_id"] = uploaded["result"]["temp_file_id"]
                    body["source_metadata"]["source"] = "temp_upload"
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
                **({"auxiliary_files": auxiliary_files} if auxiliary_files is not None else {}),
            )
            if not created.get("ok"):
                return created
        else:
            return current
        published = await self.get_skill(skill_name=skill_name)
        if package is not None and published.get("ok"):
            if not _skill_package_matches(published.get("result"), skill_body, package):
                return self._package_failure(
                    "verify shared Skill package", "OpenViking Skill package integrity mismatch",
                )
        return published

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
        if task_id.startswith("kee-memory-") and self.internal_port is not None:
            return await self.internal_port.status(self, task_id)
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
        reason: str = "",
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

        if self.internal_port is not None:
            return await self.internal_port.run_batch(self, source_uris=source_uris, target_uri=target_uri,
                skill_uri=skill_uri, skill_revision=skill_revision, reason=reason,
                runtime_timeout_seconds=runtime_timeout_seconds, last_compile_time=last_compile_time)
        operation = "POST /api/v1/compile"
        body: dict[str, Any] = {
            "from": list(source_uris),
            "to": target_uri,
            "skill": skill_uri,
        }
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
