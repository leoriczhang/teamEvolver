"""HTTP client for the Langfuse v3 public REST API (verified: v3.117.2).

Uses ``httpx`` (already a teamEvolver dependency) so we do not couple to a
particular ``langfuse`` SDK release. Authentication is HTTP Basic with the
project public key as username and secret key as password, exactly as the
Langfuse public API expects.

Endpoints used (all under ``{host}/api/public``):
- ``GET /sessions``               list sessions (page, limit, from/toTimestamp, environment)
- ``GET /sessions/{id}``          one session incl. its traces
- ``GET /traces``                 list traces with rich attribute filters
- ``GET /traces/{id}``            one trace incl. observations + scores

Session-attribute filtering is the headline capability: the ``/sessions`` list
endpoint only filters by time + environment, but agent sessions are usually
tagged at the *trace* level (userId, tags, release, version, name, metadata).
:meth:`LangfuseClient.list_session_ids` therefore resolves the set of matching
session ids through the far richer ``/traces`` endpoint whenever any
trace-level filter is supplied, and falls back to ``/sessions`` otherwise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "https://cloud.langfuse.com"


class LangfuseError(RuntimeError):
    """Raised when the Langfuse API is unreachable or returns an error."""


@dataclass
class SessionFilters:
    """Filter set for selecting Langfuse sessions to pull.

    Time + ``environment`` map to native ``/sessions`` query params. The
    remaining fields are trace-level attributes; when any of them is set the
    client resolves session ids via the ``/traces`` endpoint (which supports
    them) instead of the limited ``/sessions`` list.
    """

    from_timestamp: str = ""
    to_timestamp: str = ""
    environment: list[str] = field(default_factory=list)
    user_id: str = ""
    tags: list[str] = field(default_factory=list)
    release: str = ""
    version: str = ""
    trace_name: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_trace_level_filter(self) -> bool:
        return bool(
            self.user_id
            or self.tags
            or self.release
            or self.version
            or self.trace_name
            or self.metadata
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_timestamp": self.from_timestamp,
            "to_timestamp": self.to_timestamp,
            "environment": list(self.environment),
            "user_id": self.user_id,
            "tags": list(self.tags),
            "release": self.release,
            "version": self.version,
            "trace_name": self.trace_name,
            "session_id": self.session_id,
            "metadata": dict(self.metadata),
        }


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so we never send blank query params."""
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value in (None, "", [], {}):
            continue
        cleaned[key] = value
    return cleaned


class LangfuseClient:
    """Thin, synchronous client over the Langfuse public REST API."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = 30.0,
        page_limit: int = 50,
    ) -> None:
        host = str(host or _DEFAULT_HOST).strip().rstrip("/")
        if not host:
            host = _DEFAULT_HOST
        if not public_key or not secret_key:
            raise LangfuseError(
                "Langfuse public_key and secret_key are required. "
                "Set langfuse.public_key and langfuse.secret_key."
            )
        self._base_url = f"{host}/api/public"
        self._auth = (public_key, secret_key)
        self._timeout = float(timeout or 30.0)
        self._page_limit = max(1, min(100, int(page_limit or 50)))

    @classmethod
    def from_config(cls, config) -> "LangfuseClient":
        return cls(
            host=str(getattr(config, "langfuse_host", "") or _DEFAULT_HOST),
            public_key=str(getattr(config, "langfuse_public_key", "") or ""),
            secret_key=str(getattr(config, "langfuse_secret_key", "") or ""),
            timeout=float(getattr(config, "langfuse_timeout_seconds", 30) or 30),
            page_limit=int(getattr(config, "langfuse_page_limit", 50) or 50),
        )

    # ------------------------------------------------------------------ #
    # Low-level request helper                                            #
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(url, params=_clean_params(params or {}), auth=self._auth)
        except httpx.HTTPError as exc:
            raise LangfuseError(f"Langfuse request to {path} failed: {exc}") from exc
        if response.status_code == 401:
            raise LangfuseError(
                "Langfuse authentication failed (401). Check langfuse.public_key / secret_key."
            )
        if response.status_code >= 400:
            snippet = response.text[:300]
            raise LangfuseError(
                f"Langfuse GET {path} returned HTTP {response.status_code}: {snippet}"
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LangfuseError(f"Langfuse GET {path} returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def health(self) -> dict[str, Any]:
        """Ping the API with a minimal sessions request to verify credentials."""
        payload = self._get("/sessions", {"page": 1, "limit": 1})
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        return {
            "ok": True,
            "host": self._base_url,
            "total_sessions": meta.get("totalItems"),
        }

    # ------------------------------------------------------------------ #
    # Sessions                                                            #
    # ------------------------------------------------------------------ #
    def iter_sessions(
        self,
        *,
        from_timestamp: str = "",
        to_timestamp: str = "",
        environment: Optional[Iterable[str]] = None,
        max_items: int = 0,
    ) -> list[dict[str, Any]]:
        """List sessions via ``GET /api/public/sessions`` with time/env filters."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "page": page,
                "limit": self._page_limit,
                "fromTimestamp": from_timestamp,
                "toTimestamp": to_timestamp,
            }
            envs = [str(e).strip() for e in (environment or []) if str(e).strip()]
            if envs:
                params["environment"] = envs
            payload = self._get("/sessions", params)
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            for item in data:
                if isinstance(item, dict):
                    results.append(item)
                    if max_items and len(results) >= max_items:
                        return results
            if not self._has_next_page(payload, page, len(data)):
                break
            page += 1
        return results

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Fetch one session (incl. its traces) via ``/sessions/{id}``."""
        session_id = str(session_id or "").strip()
        if not session_id:
            raise LangfuseError("session_id is required")
        return self._get(f"/sessions/{session_id}")

    # ------------------------------------------------------------------ #
    # Traces                                                              #
    # ------------------------------------------------------------------ #
    def iter_traces(
        self,
        *,
        session_id: str = "",
        user_id: str = "",
        name: str = "",
        from_timestamp: str = "",
        to_timestamp: str = "",
        tags: Optional[Iterable[str]] = None,
        version: str = "",
        release: str = "",
        environment: Optional[Iterable[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        order_by: str = "timestamp.asc",
        max_items: int = 0,
    ) -> list[dict[str, Any]]:
        """List traces via ``GET /api/public/traces`` with attribute filters.

        ``metadata`` (dict of key->value) is translated into the advanced
        ``filter`` JSON param (``stringObject`` conditions), which Langfuse v3
        supports for trace listing.
        """
        results: list[dict[str, Any]] = []
        page = 1
        tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
        env_list = [str(e).strip() for e in (environment or []) if str(e).strip()]
        filter_json = self._build_metadata_filter(metadata)
        while True:
            params: dict[str, Any] = {
                "page": page,
                "limit": self._page_limit,
                "sessionId": session_id,
                "userId": user_id,
                "name": name,
                "fromTimestamp": from_timestamp,
                "toTimestamp": to_timestamp,
                "version": version,
                "release": release,
                "orderBy": order_by,
            }
            if tag_list:
                params["tags"] = tag_list
            if env_list:
                params["environment"] = env_list
            if filter_json:
                params["filter"] = filter_json
            payload = self._get("/traces", params)
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            for item in data:
                if isinstance(item, dict):
                    results.append(item)
                    if max_items and len(results) >= max_items:
                        return results
            if not self._has_next_page(payload, page, len(data)):
                break
            page += 1
        return results

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        """Fetch one trace incl. observations + scores via ``/traces/{id}``."""
        trace_id = str(trace_id or "").strip()
        if not trace_id:
            raise LangfuseError("trace_id is required")
        return self._get(f"/traces/{trace_id}")

    # ------------------------------------------------------------------ #
    # Session-id resolution with attribute filtering                     #
    # ------------------------------------------------------------------ #
    def list_session_ids(self, filters: SessionFilters, *, max_sessions: int = 0) -> list[str]:
        """Return matching session ids, honoring trace-level attribute filters.

        When ``filters`` includes any trace-level attribute the ids are derived
        from the ``/traces`` endpoint (which supports them); otherwise the
        lighter ``/sessions`` list is used. Preserves first-seen order.
        """
        if filters.session_id:
            return [filters.session_id]

        ordered: list[str] = []
        seen: set[str] = set()

        if filters.has_trace_level_filter():
            # Fetch enough traces to surface up to max_sessions distinct
            # sessions; a session usually has several traces, so scale the cap.
            trace_cap = (max_sessions * 20) if max_sessions else 0
            traces = self.iter_traces(
                user_id=filters.user_id,
                name=filters.trace_name,
                from_timestamp=filters.from_timestamp,
                to_timestamp=filters.to_timestamp,
                tags=filters.tags,
                version=filters.version,
                release=filters.release,
                environment=filters.environment,
                metadata=filters.metadata,
                max_items=trace_cap,
            )
            for trace in traces:
                sid = str(trace.get("sessionId") or "").strip()
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                ordered.append(sid)
                if max_sessions and len(ordered) >= max_sessions:
                    break
            return ordered

        sessions = self.iter_sessions(
            from_timestamp=filters.from_timestamp,
            to_timestamp=filters.to_timestamp,
            environment=filters.environment,
            max_items=max_sessions,
        )
        for session in sessions:
            sid = str(session.get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            ordered.append(sid)
            if max_sessions and len(ordered) >= max_sessions:
                break
        return ordered

    def fetch_session_with_traces(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (session, full_traces) with observations resolved per trace.

        The ``/sessions/{id}`` response lists traces but WITHOUT observations,
        so each trace is re-fetched via ``/traces/{id}`` to obtain the
        observation-level token/tool metrics the converter needs.
        """
        session = self.get_session(session_id)
        raw_traces = session.get("traces") if isinstance(session.get("traces"), list) else []
        full_traces: list[dict[str, Any]] = []
        for trace in raw_traces:
            if not isinstance(trace, dict):
                continue
            trace_id = str(trace.get("id") or "").strip()
            if not trace_id:
                continue
            try:
                full = self.get_trace(trace_id)
            except LangfuseError as exc:
                logger.warning("[Langfuse] failed to fetch trace %s: %s", trace_id, exc)
                full = trace
            # Preserve the session id even if the detail response omits it.
            full.setdefault("sessionId", trace.get("sessionId") or session_id)
            full_traces.append(full)
        return session, full_traces

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_metadata_filter(metadata: Optional[dict[str, Any]]) -> str:
        if not isinstance(metadata, dict) or not metadata:
            return ""
        conditions: list[dict[str, Any]] = []
        for key, value in metadata.items():
            key = str(key).strip()
            if not key:
                continue
            if isinstance(value, bool):
                conditions.append(
                    {
                        "type": "booleanObject",
                        "column": "metadata",
                        "key": key,
                        "operator": "=",
                        "value": value,
                    }
                )
            elif isinstance(value, (int, float)):
                conditions.append(
                    {
                        "type": "numberObject",
                        "column": "metadata",
                        "key": key,
                        "operator": "=",
                        "value": value,
                    }
                )
            else:
                conditions.append(
                    {
                        "type": "stringObject",
                        "column": "metadata",
                        "key": key,
                        "operator": "=",
                        "value": str(value),
                    }
                )
        return json.dumps(conditions, ensure_ascii=False) if conditions else ""

    @staticmethod
    def _has_next_page(payload: dict[str, Any], page: int, batch_size: int) -> bool:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total_pages = meta.get("totalPages")
        try:
            if total_pages is not None:
                return page < int(total_pages)
        except (TypeError, ValueError):
            pass
        # Fall back to a full-page heuristic when meta is missing.
        limit = meta.get("limit")
        try:
            limit_int = int(limit) if limit is not None else 0
        except (TypeError, ValueError):
            limit_int = 0
        return bool(batch_size and limit_int and batch_size >= limit_int)
