"""Typed client for OpenViking's Git-backed Snapshot API."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_CONFLICT_CODES = {
    "CONFLICT",
    "GIT_CONCURRENT_COMMIT",
    "RESOURCE_BUSY",
}
_NOT_FOUND_CODES = {
    "NOT_FOUND",
    "RESOURCE_NOT_FOUND",
}
_UNAVAILABLE_CODES = {
    "AGFS_NOT_SUPPORTED",
    "FEATURE_DISABLED",
    "NOT_SUPPORTED",
    "UNAVAILABLE",
}


class SnapshotError(RuntimeError):
    """Base error with the stable OpenViking code and response details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


class SnapshotConflictError(SnapshotError):
    """The account branch changed concurrently."""


class SnapshotNotFoundError(SnapshotError):
    """The requested ref or blob does not exist."""


class SnapshotUnavailableError(SnapshotError):
    """Git Snapshot is disabled or unavailable on this deployment."""


class SnapshotPartialRestoreError(SnapshotError):
    """Restore advanced HEAD but could not write every VFS path."""


class SnapshotProtocolError(SnapshotError):
    """OpenViking returned a malformed Snapshot response."""


@dataclass(frozen=True)
class SnapshotBlob:
    oid: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class OpenVikingSnapshotClient:
    """Small synchronous client that exposes no mutating restore operation."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str = "",
        account: str = "default",
        user: str = "default",
        agent: str = "teamEvolver-snapshot",
        branch: str = "main",
        timeout: float = 30.0,
        http_client: Any | None = None,
    ) -> None:
        if not str(endpoint or "").strip():
            raise ValueError("OpenViking Snapshot requires an endpoint")
        if not str(branch or "").strip():
            raise ValueError("OpenViking Snapshot requires a branch")
        self._endpoint = str(endpoint).rstrip("/")
        self._account = str(account or "default")
        self._user = str(user or "default")
        self._agent = str(agent or "teamEvolver-snapshot")
        self._branch = str(branch)
        self._timeout = float(timeout)
        self._owns_client = http_client is None
        if http_client is None:
            import httpx

            http_client = httpx.Client(
                base_url=self._endpoint,
                headers=self._headers(api_key),
                timeout=self._timeout,
            )
        self._http = http_client

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "X-OpenViking-Account": self._account,
            "X-OpenViking-User": self._user,
            "X-OpenViking-Agent": self._agent,
        }
        if api_key:
            headers["X-API-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @property
    def branch(self) -> str:
        return self._branch

    @property
    def account_hash(self) -> str:
        return hashlib.sha256(self._account.encode("utf-8")).hexdigest()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def commit(
        self,
        *,
        message: str,
        paths: Iterable[str] | None = None,
        author_name: str = "teamEvolver",
        author_email: str = "teamEvolver@localhost",
    ) -> dict[str, Any]:
        if not str(message or "").strip():
            raise ValueError("Snapshot commit message is required")
        normalized_paths = (
            self._normalize_paths(paths)
            if paths is not None
            else None
        )
        body: dict[str, Any] = {
            "message": str(message),
            "branch": self._branch,
            "author_name": str(author_name),
            "author_email": str(author_email),
        }
        if normalized_paths is not None:
            body["paths"] = normalized_paths
        result = self._request_json(
            "POST",
            "/api/v1/snapshot/commit",
            json=body,
        )
        if not isinstance(result, dict):
            raise self._protocol_error("Snapshot commit returned a non-object")
        self._require_response_oid(result.get("commit_oid"), "commit_oid")
        return dict(result)

    def log(
        self,
        *,
        limit: int = 20,
        paths: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("branch", self._branch),
            ("limit", max(1, min(500, int(limit)))),
        ]
        for path in self._normalize_paths(paths or []):
            params.append(("paths", path))
        result = self._request_json(
            "GET",
            "/api/v1/snapshot/log",
            params=params,
        )
        if not isinstance(result, list):
            raise self._protocol_error("Snapshot log returned a non-list")
        return [dict(item) for item in result if isinstance(item, dict)]

    def show_metadata(self, target_ref: str | None = None) -> dict[str, Any]:
        result = self._request_json(
            "GET",
            "/api/v1/snapshot/show",
            params={"target_ref": str(target_ref or self._branch)},
        )
        if not isinstance(result, dict):
            raise self._protocol_error("Snapshot show returned a non-object")
        self._require_response_oid(result.get("oid"), "oid")
        return dict(result)

    def show_blob(
        self,
        target_ref: str,
        *,
        path: str,
        raw: bool = True,
    ) -> SnapshotBlob:
        self._require_oid(target_ref, "target_ref")
        self._require_uri(path)
        response = self._http.request(
            "GET",
            "/api/v1/snapshot/show",
            params={
                "target_ref": target_ref,
                "path": path,
                "raw": str(bool(raw)).lower(),
            },
            timeout=self._timeout,
        )
        self._raise_for_response(response)
        oid = str(response.headers.get("X-Snapshot-Oid") or "")
        self._require_response_oid(oid, "X-Snapshot-Oid")
        content = bytes(response.content)
        size_header = response.headers.get("X-Snapshot-Size")
        if size_header is not None and int(size_header) != len(content):
            raise self._protocol_error("Snapshot blob size header mismatch")
        return SnapshotBlob(oid=oid, content=content)

    def diff(
        self,
        *,
        path: str,
        from_ref: str | None,
        to_ref: str,
        raw: bool = False,
    ) -> dict[str, Any]:
        self._require_uri(path)
        self._require_oid(to_ref, "to_ref")
        params: dict[str, Any] = {
            "path": path,
            "to": to_ref,
            "raw": str(bool(raw)).lower(),
        }
        if from_ref:
            self._require_oid(from_ref, "from_ref")
            params["from"] = from_ref
        result = self._request_json(
            "GET",
            "/api/v1/snapshot/diff",
            params=params,
        )
        if not isinstance(result, dict):
            raise self._protocol_error("Snapshot diff returned a non-object")
        return dict(result)

    def restore_dry_run(
        self,
        *,
        source_commit: str,
        project_dir: str | None = None,
    ) -> dict[str, Any]:
        self._require_oid(source_commit, "source_commit")
        body: dict[str, Any] = {
            "source_commit": source_commit,
            "branch": self._branch,
            "dry_run": True,
        }
        if project_dir:
            self._require_uri(project_dir)
            body["project_dir"] = project_dir
        result = self._request_json(
            "POST",
            "/api/v1/snapshot/restore",
            json=body,
        )
        if not isinstance(result, dict):
            raise self._protocol_error("Snapshot restore dry-run returned a non-object")
        return dict(result)

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        response = self._http.request(method, path, **kwargs)
        self._raise_for_response(response)
        try:
            payload = response.json()
        except Exception as exc:
            raise self._protocol_error("Snapshot response is not JSON") from exc
        if isinstance(payload, dict) and payload.get("status") == "error":
            self._raise_payload_error(payload, response.status_code)
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def _raise_for_response(self, response: Any) -> None:
        if int(response.status_code) < 400:
            return
        try:
            payload = response.json()
        except Exception:
            payload = {}
        self._raise_payload_error(
            payload if isinstance(payload, dict) else {},
            int(response.status_code),
            fallback=str(getattr(response, "text", "") or "")[:500],
        )

    def _raise_payload_error(
        self,
        payload: Mapping[str, Any],
        status_code: int,
        *,
        fallback: str = "",
    ) -> None:
        raw = payload.get("error") or payload.get("detail") or {}
        if isinstance(raw, Mapping):
            code = str(raw.get("code") or f"HTTP_{status_code}")
            message = str(raw.get("message") or fallback or "Snapshot request failed")
            details = raw.get("details")
        else:
            code = f"HTTP_{status_code}"
            message = str(raw or fallback or "Snapshot request failed")
            details = None
        normalized = code.upper()
        error_type: type[SnapshotError]
        if normalized in _CONFLICT_CODES or status_code == 409:
            error_type = SnapshotConflictError
        elif normalized in _NOT_FOUND_CODES or status_code == 404:
            error_type = SnapshotNotFoundError
        elif normalized in _UNAVAILABLE_CODES or status_code in {501, 503}:
            error_type = SnapshotUnavailableError
        elif normalized == "RESTORE_WRITEBACK_PARTIAL":
            error_type = SnapshotPartialRestoreError
        else:
            error_type = SnapshotError
        raise error_type(
            code,
            message,
            status_code=status_code,
            details=details if isinstance(details, Mapping) else None,
        )

    @staticmethod
    def _normalize_paths(paths: Iterable[str]) -> list[str]:
        output: list[str] = []
        for raw in paths:
            value = str(raw or "").strip().rstrip("/")
            OpenVikingSnapshotClient._require_uri(value)
            if value not in output:
                output.append(value)
        return output

    @staticmethod
    def _require_uri(value: str) -> None:
        if not str(value or "").startswith("viking://"):
            raise ValueError(f"Snapshot path must be a viking:// URI: {value!r}")

    @staticmethod
    def _require_oid(value: Any, field: str) -> str:
        oid = str(value or "").lower()
        if not _OID_RE.fullmatch(oid):
            raise ValueError(f"Snapshot {field} must be a full 40-character OID")
        return oid

    def _require_response_oid(self, value: Any, field: str) -> str:
        try:
            return self._require_oid(value, field)
        except ValueError as exc:
            raise self._protocol_error(
                f"Snapshot response {field} is not a full OID"
            ) from exc

    @staticmethod
    def _protocol_error(message: str) -> SnapshotProtocolError:
        return SnapshotProtocolError("INVALID_RESPONSE", message)
