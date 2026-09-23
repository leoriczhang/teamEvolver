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
import re
from typing import Any, Awaitable, Callable, Optional

from .langfuse_client import LangfuseClient, LangfuseError, SessionFilters
from .langfuse_convert import convert_langfuse_session
from .langfuse_mapper import MapperRegistry
from .langfuse_mapper import build_mapper_registry as build_mapper_registry

logger = logging.getLogger(__name__)


def sanitize_session_id(value: Any) -> str:
    """Normalize a Langfuse session id for object-storage keys.

    Mirrors the proxy's ``_safe_session_id`` so ids written by the in-process
    CLI pull path match what the dashboard detail lookup resolves (Langfuse ids
    commonly contain ``:`` which is unsafe for storage keys).
    """
    raw = str(value or "").strip()
    if not raw:
        return "session"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-/")[:160] or "session"


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
    filters = build_filters_from_config(config, overrides)
    cap = max_sessions or int(getattr(config, "langfuse_max_sessions", 100) or 100)
    if not 1 <= cap <= 1000:
        raise LangfuseError("max_sessions must be between 1 and 1000; narrow the time window for larger pulls")
    source_type = str(getattr(config, "datasource_type", "") or "").strip().lower()
    if source_type == "skillopt":
        from .source_adapter import build_source_adapter

        adapter = build_source_adapter(config)
        try:
            ids = adapter.list_session_ids(filters.as_dict(), max_sessions=cap)
            rows = []
            for sid in ids:
                first = adapter.meta[adapter.sessions[sid][0]]
                rows.append({"session_id": sid, "title": first.get("first_text") or first.get("name") or "",
                             "timestamp": first.get("timestamp"), "user_id": first.get("emp_id", ""),
                             "trace_count": len(adapter.sessions[sid])})
            return {"filters": filters.as_dict(), "count": len(rows), "sessions": rows}
        finally:
            adapter.close()
    if source_type not in ("", "langfuse"):
        # File-based / registered custom source adapters: route the preview
        # through the adapter itself. Adapters may implement an optional
        # preview_sessions(filters, max_sessions) returning rich rows;
        # otherwise fall back to minimal rows from list_session_ids.
        from .source_adapter import build_source_adapter

        adapter = build_source_adapter(config)
        try:
            preview_fn = getattr(adapter, "preview_sessions", None)
            rows: list[dict[str, Any]] = []
            if callable(preview_fn):
                rows = [
                    row if isinstance(row, dict) else {"session_id": str(row)}
                    for row in (preview_fn(filters.as_dict(), max_sessions=cap) or [])
                ]
            else:
                rows = [
                    {"session_id": sid}
                    for sid in adapter.list_session_ids(filters.as_dict(), max_sessions=cap)
                ]
        finally:
            if hasattr(adapter, "close"):
                adapter.close()
        return {"filters": filters.as_dict(), "count": len(rows), "sessions": rows}
    client = LangfuseClient.from_config(config)
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
    concurrency: int = 0,
    require_used_skills: Optional[list[str]] = None,
    agent_id: str = "",
) -> dict[str, Any]:
    """Pull, convert, and ingest matching sessions from the configured data source.

    ``ingest`` is an async callable taking one teamEvolver session dict and
    returning its status payload (``{"status": "queued"|"skipped"|...}``). This
    keeps the transport (in-process vs HTTP) out of the orchestration.

    Sessions are processed with bounded concurrency (default 8, env
    ``TEAMEVOLVER_LANGFUSE_PULL_CONCURRENCY``): the fetch (sync httpx) runs on
    worker threads and the per-session pipelines overlap, so a large pull is
    bound by the LLM classifier rather than by a serial fetch loop. The
    ``results`` list preserves the ``session_ids`` order regardless of
    completion order.

    ``require_used_skills`` optionally filters ingested sessions to those whose
    converted session-level ``used_skills`` (mapper-filled) include at least
    one of the listed skills; non-matching sessions are reported as
    ``filtered`` and never reach ``ingest``. Used for tenant onboarding pulls
    like "only sessions that exercised skill X".

    ``agent_id`` optionally loads per-agent hooks from ``session_ingestion/adapters/<agent_id>.py``
    (filter_session, extract_fields, dedup, map_trace, map_session). Hooks are
    hot-reloaded by mtime; failures fall back to defaults (fail-open).
    """
    import asyncio
    import os

    from .source_adapter import (
        AdapterContext,
        build_source_adapter,
        resolve_hooks,
        run_extract_fields,
        run_filter_session,
        run_map_session,
        run_map_trace,
    )

    _ensure_enabled(config)
    adapter = build_source_adapter(config)
    filters = build_filters_from_config(config, overrides)
    cap = max_sessions or int(getattr(config, "langfuse_max_sessions", 100) or 100)
    if not 1 <= cap <= 1000:
        raise LangfuseError("max_sessions must be between 1 and 1000; narrow the time window for larger pulls")

    # Per-agent file-based hooks (hot-reloaded, fail-open).
    # When agent_id is empty, resolve_hooks returns all-defaults (no overrides).
    agent_hooks = resolve_hooks(agent_id, config)

    required_skills = [str(s).strip() for s in (require_used_skills or []) if str(s or "").strip()]

    filter_dict = filters.as_dict() if hasattr(filters, "as_dict") else {}
    try:
        session_ids = await asyncio.to_thread(adapter.list_session_ids, filter_dict, max_sessions=cap)
    except BaseException:
        if hasattr(adapter, "close"):
            await asyncio.to_thread(adapter.close)
        raise

    if concurrency <= 0:
        try:
            concurrency = int(os.environ.get("TEAMEVOLVER_LANGFUSE_PULL_CONCURRENCY", "8"))
        except ValueError:
            concurrency = 8
    semaphore = asyncio.Semaphore(max(1, concurrency))

    # Build the adapter context for hook calls.
    hook_ctx = AdapterContext(
        agent_id=agent_id,
        source_type=getattr(adapter, "source_type", "langfuse"),
        config=config,
        filters=filter_dict,
    )

    async def _process(session_id: str) -> dict[str, Any]:
        async with semaphore:
            try:
                session, traces = await asyncio.to_thread(
                    adapter.fetch_session, session_id
                )
                # Step 1: built-in conversion (convert_trace_to_turn per trace).
                converted = await asyncio.to_thread(
                    adapter.convert_session, session, traces
                )
            except LangfuseError as exc:
                logger.warning("[SourceAdapter] failed to fetch session %s: %s", session_id, exc)
                return {"session_id": session_id, "status": "error", "reason": str(exc)}
            except Exception as exc:  # noqa: BLE001 — custom adapter may raise
                logger.warning("[SourceAdapter] failed to fetch session %s: %s", session_id, exc)
                return {"session_id": session_id, "status": "error", "reason": str(exc)}

            # Step 2: per-agent hooks (all fail-open).
            # map_trace: per-trace override (deep-merged over built-in turn).
            if agent_hooks.map_trace is not None:
                new_turns = []
                for i, turn in enumerate(converted.get("turns") or [], 1):
                    trace = traces[i - 1] if i <= len(traces) else {}
                    obs = trace.get("observations") if isinstance(trace, dict) else []
                    new_turn = run_map_trace(
                        agent_hooks, trace, obs or [], i, turn
                    )
                    new_turns.append(new_turn)
                converted["turns"] = new_turns

                # Re-aggregate session-level skill unions from the updated turns,
                # since map_trace may have changed per-turn used_skills/injected_skills.
                _injected: list[str] = []
                _used: list[str] = []
                for t in converted.get("turns") or []:
                    for key, bucket in (("injected_skills", _injected), ("used_skills", _used)):
                        for skill in (t.get(key) or []):
                            skill = str(skill).strip()
                            if skill and skill not in bucket:
                                bucket.append(skill)
                converted["injected_skills"] = _injected
                converted["used_skills"] = _used

            # filter_session: return None to drop.
            filtered = run_filter_session(agent_hooks, converted, hook_ctx)
            if filtered is None:
                return {"session_id": session_id, "status": "filtered", "reason": "adapter filter_session dropped"}
            converted = filtered

            # extract_fields: merge extra fields into the session.
            extra = run_extract_fields(agent_hooks, converted, hook_ctx)
            if extra:
                converted.update(extra)

            # map_session: post-conversion session hook (deep-merge).
            converted = run_map_session(agent_hooks, converted, session, traces)

            if not _has_meaningful_content(converted):
                return {"session_id": session_id, "status": "empty"}

            if required_skills:
                session_skills = {
                    str(s).strip() for s in converted.get("used_skills") or []
                }
                if not session_skills.intersection(required_skills):
                    return {
                        "session_id": session_id,
                        "status": "filtered",
                        "reason": "required skill not used",
                        "used_skills": sorted(session_skills),
                    }

            if user_alias and not converted.get("user_alias"):
                converted["user_alias"] = user_alias
            converted.setdefault("user_alias", user_alias or "langfuse")
            # The ingest contract requires a pre-sanitized session_id (storage keys
            # are derived from it); keep the original Langfuse id in metadata.
            raw_langfuse_id = str(converted.get("session_id") or "")
            converted["session_id"] = sanitize_session_id(raw_langfuse_id)
            if raw_langfuse_id and raw_langfuse_id != converted["session_id"]:
                converted.setdefault("metadata", {})
                if isinstance(converted["metadata"], dict):
                    converted["metadata"].setdefault("langfuse_session_id", raw_langfuse_id)
            if force_reprocess:
                converted["force_reprocess"] = True
                converted["reprocess_reason"] = "langfuse pull force_reprocess"
            if defer_evolution_trigger:
                converted["defer_evolution_trigger"] = True

            try:
                outcome = await ingest(converted)
            except Exception as exc:  # noqa: BLE001 - ingest transport errors are per-session
                logger.warning("[Langfuse] ingest failed for session %s: %s", session_id, exc)
                return {"session_id": session_id, "status": "error", "reason": str(exc)}

            status = str(outcome.get("status") or "")
            return {
                "session_id": session_id,
                "status": status or "unknown",
                "queued": bool(outcome.get("queued")),
                "turns": len(converted.get("turns") or []),
                "value_judge": outcome.get("value_judge"),
            }

    # gather preserves session_ids order in the returned list.
    try:
        results: list[dict[str, Any]] = list(await asyncio.gather(*(_process(sid) for sid in session_ids)))
    finally:
        if hasattr(adapter, "close"):
            await asyncio.to_thread(adapter.close)
    counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    for key in ("queued", "skipped", "duplicate", "empty", "error", "filtered"):
        counts.setdefault(key, 0)

    return {
        "filters": filters.as_dict(),
        "total": len(session_ids),
        "counts": counts,
        "results": results,
    }


def convert_session_with_registry(
    session: dict[str, Any],
    traces: list[dict[str, Any]],
    registry: Optional[MapperRegistry],
) -> dict[str, Any]:
    """Convert one session with per-trace registry routing + session hooks.

    Fail-open at every level: a ``None`` registry (or a registry that raises)
    yields the built-in conversion; per-trace mapper errors are already
    absorbed inside :meth:`MapperRegistry.map_trace`. Session hooks run after
    conversion and win over the caller-supplied ``user_alias`` default —
    :func:`pull_sessions` fills that default only when the field is still
    empty after this helper returns.
    """
    if registry is None:
        return convert_langfuse_session(session, traces)
    try:
        converted = convert_langfuse_session(session, traces, mapper=registry)
    except Exception as exc:  # noqa: BLE001 - operator mapper can raise anything
        logger.warning(
            "[Langfuse] mapper registry failed for session %s; using built-in mapping: %s",
            session.get("id") or session.get("session_id") or "?",
            exc,
        )
        return convert_langfuse_session(session, traces)
    try:
        return registry.apply_session_hooks(converted, session, traces)
    except Exception as exc:  # noqa: BLE001 - operator hooks can raise anything
        logger.warning(
            "[Langfuse] session hooks failed for session %s; keeping un-hooked result: %s",
            session.get("id") or session.get("session_id") or "?",
            exc,
        )
        return converted


def _convert_session(
    session: dict[str, Any],
    traces: list[dict[str, Any]],
    trace_mapper: Optional[MapperRegistry],
) -> dict[str, Any]:
    """Convert one session, falling back to the built-in mapping on mapper error.

    A custom mapper is operator-authored and may raise on an unexpected trace
    shape. Rather than fail the whole pull, we log once and re-run the built-in
    conversion for that session so ingestion still proceeds.
    """
    return convert_session_with_registry(session, traces, trace_mapper)


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
