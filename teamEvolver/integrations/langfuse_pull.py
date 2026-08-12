"""Pull agent sessions from Langfuse into the teamEvolver evolution pipeline.

This is the orchestration layer that stitches together:
  1. :class:`~teamEvolver.integrations.langfuse_client.LangfuseClient` — the
     Langfuse v3 public REST client (with session-attribute filtering), and
  2. :mod:`~teamEvolver.integrations.langfuse_convert` — the pure mapping from
     Langfuse sessions/traces to the teamEvolver session dict.

Two entry points are provided:
  - :func:`build_filters_from_config` merges configured default filters with
    per-request overrides into a single :class:`SessionFilters`.
  - :func:`preview_sessions` lists matching sessions (ids + light metadata)
    without ingesting — used by the dashboard "list" view and CLI ``list``.
  - :func:`pull_sessions` converts each matching session and hands it to an
    ``ingest`` callable (the in-process ingest helper for the REST endpoint, or
    an HTTP POST for the CLI), returning a per-session summary.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from .langfuse_client import LangfuseClient, LangfuseError, SessionFilters
from .langfuse_convert import convert_langfuse_session

logger = logging.getLogger(__name__)


def build_filters_from_config(config, overrides: Optional[dict[str, Any]] = None) -> SessionFilters:
    """Combine configured default filters with per-pull overrides.

    Overrides win over config defaults. List-valued fields (``environment``,
    ``tags``) accept a list or a comma-separated string.
    """
    overrides = overrides or {}

    def _as_list(value: Any, fallback: Any) -> list[str]:
        source = value if value not in (None, "", [], {}) else fallback
        if isinstance(source, (list, tuple, set)):
            items = source
        elif source in (None, ""):
            items = []
        else:
            items = str(source).replace("\n", ",").split(",")
        return [item for raw in items if (item := str(raw or "").strip())]

    def _as_str(value: Any, fallback: Any) -> str:
        if value not in (None, ""):
            return str(value).strip()
        return str(fallback or "").strip()

    metadata = overrides.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    return SessionFilters(
        from_timestamp=_as_str(overrides.get("from_timestamp"), ""),
        to_timestamp=_as_str(overrides.get("to_timestamp"), ""),
        environment=_as_list(
            overrides.get("environment"),
            getattr(config, "langfuse_default_environment", []),
        ),
        user_id=_as_str(
            overrides.get("user_id"), getattr(config, "langfuse_default_user_id", "")
        ),
        tags=_as_list(overrides.get("tags"), getattr(config, "langfuse_default_tags", [])),
        release=_as_str(
            overrides.get("release"), getattr(config, "langfuse_default_release", "")
        ),
        version=_as_str(
            overrides.get("version"), getattr(config, "langfuse_default_version", "")
        ),
        trace_name=_as_str(
            overrides.get("trace_name"), getattr(config, "langfuse_default_trace_name", "")
        ),
        session_id=_as_str(overrides.get("session_id"), ""),
        metadata=metadata,
    )


def _ensure_enabled(config) -> None:
    if not bool(getattr(config, "langfuse_enabled", False)):
        raise LangfuseError(
            "Langfuse integration is disabled. Enable it with "
            "'teamEvolver config langfuse.enabled true' and set host/keys."
        )


def preview_sessions(
    config,
    overrides: Optional[dict[str, Any]] = None,
    *,
    max_sessions: int = 0,
) -> dict[str, Any]:
    """List matching Langfuse sessions without ingesting them.

    Returns ``{"filters", "count", "sessions": [{session_id, ...light meta}]}``.
    Light metadata is derived from the session + a cheap trace listing so the
    caller can display attributes (user, tags, environment, trace count) without
    fetching every observation.
    """
    _ensure_enabled(config)
    client = LangfuseClient.from_config(config)
    filters = build_filters_from_config(config, overrides)
    cap = max_sessions or int(getattr(config, "langfuse_max_sessions", 100) or 100)
    session_ids = client.list_session_ids(filters, max_sessions=cap)

    sessions: list[dict[str, Any]] = []
    for session_id in session_ids:
        meta: dict[str, Any] = {"session_id": session_id}
        try:
            traces = client.iter_traces(session_id=session_id, max_items=200)
        except LangfuseError as exc:
            logger.warning("[Langfuse] preview trace list failed for %s: %s", session_id, exc)
            traces = []
        if traces:
            first = traces[0]
            meta.update(
                {
                    "title": str(first.get("name") or ""),
                    "timestamp": str(first.get("timestamp") or ""),
                    "trace_count": len(traces),
                    "user_id": _first_attr(traces, "userId"),
                    "environment": _first_attr(traces, "environment"),
                    "release": _first_attr(traces, "release"),
                    "version": _first_attr(traces, "version"),
                    "tags": _collect_tags(traces),
                }
            )
        sessions.append(meta)

    return {
        "filters": filters.as_dict(),
        "count": len(sessions),
        "sessions": sessions,
    }


async def pull_sessions(
    config,
    ingest: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    overrides: Optional[dict[str, Any]] = None,
    *,
    max_sessions: int = 0,
    user_alias: str = "",
    force_reprocess: bool = False,
    defer_evolution_trigger: bool = False,
) -> dict[str, Any]:
    """Pull, convert, and ingest matching Langfuse sessions.

    ``ingest`` is an async callable taking one teamEvolver session dict and
    returning its status payload (``{"status": "queued"|"skipped"|...}``). This
    keeps the transport (in-process vs HTTP) out of the orchestration.
    """
    _ensure_enabled(config)
    client = LangfuseClient.from_config(config)
    filters = build_filters_from_config(config, overrides)
    cap = max_sessions or int(getattr(config, "langfuse_max_sessions", 100) or 100)

    session_ids = client.list_session_ids(filters, max_sessions=cap)
    results: list[dict[str, Any]] = []
    counts = {"queued": 0, "skipped": 0, "duplicate": 0, "empty": 0, "error": 0}

    for session_id in session_ids:
        try:
            session, traces = client.fetch_session_with_traces(session_id)
            converted = convert_langfuse_session(session, traces)
        except LangfuseError as exc:
            logger.warning("[Langfuse] failed to fetch session %s: %s", session_id, exc)
            counts["error"] += 1
            results.append({"session_id": session_id, "status": "error", "reason": str(exc)})
            continue

        if not _has_meaningful_content(converted):
            counts["empty"] += 1
            results.append({"session_id": session_id, "status": "empty"})
            continue

        if user_alias and not converted.get("user_alias"):
            converted["user_alias"] = user_alias
        converted.setdefault("user_alias", user_alias or "langfuse")
        if force_reprocess:
            converted["force_reprocess"] = True
            converted["reprocess_reason"] = "langfuse pull force_reprocess"
        if defer_evolution_trigger:
            converted["defer_evolution_trigger"] = True

        try:
            outcome = await ingest(converted)
        except Exception as exc:  # noqa: BLE001 - ingest transport errors are per-session
            logger.warning("[Langfuse] ingest failed for session %s: %s", session_id, exc)
            counts["error"] += 1
            results.append({"session_id": session_id, "status": "error", "reason": str(exc)})
            continue

        status = str(outcome.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
        results.append(
            {
                "session_id": session_id,
                "status": status or "unknown",
                "queued": bool(outcome.get("queued")),
                "turns": len(converted.get("turns") or []),
                "value_judge": outcome.get("value_judge"),
            }
        )

    return {
        "filters": filters.as_dict(),
        "total": len(session_ids),
        "counts": counts,
        "results": results,
    }


def _has_meaningful_content(session: dict[str, Any]) -> bool:
    """True when at least one turn carries a prompt, response, or tool activity.

    A Langfuse trace always folds into a turn, but a trace with no input/output
    and no observations produces an empty turn that is not worth ingesting.
    """
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        if str(turn.get("prompt_text") or "").strip():
            return True
        if str(turn.get("response_text") or "").strip():
            return True
        if turn.get("tool_calls") or turn.get("tool_results"):
            return True
        metrics = turn.get("metrics") if isinstance(turn.get("metrics"), dict) else {}
        if int(metrics.get("total_tokens") or 0) > 0:
            return True
    return False


def _first_attr(traces: list[dict[str, Any]], key: str) -> str:
    for trace in traces:
        value = str(trace.get(key) or "").strip()
        if value:
            return value
    return ""


def _collect_tags(traces: list[dict[str, Any]]) -> list[str]:
    tags: list[str] = []
    for trace in traces:
        for tag in trace.get("tags") or []:
            tag = str(tag).strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tags
