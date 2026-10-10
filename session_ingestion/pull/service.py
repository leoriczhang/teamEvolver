"""Streaming ingestion of standard Sessions produced by a tenant adapter."""

import asyncio
import inspect
import os
from collections.abc import Iterator
from typing import Any

from session_ingestion.adapters._runtime import (
    AdapterError,
    apply_session_id_exclusions,
    describe,
    filters_for,
    load,
)
from session_ingestion.identifiers import InvalidSessionId, sanitize_session_id


def _positive_env(name: str, default: int, *, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(1, min(maximum, value))


def _next_batch(iterator: Iterator) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return None


async def _session_id_batches(adapter, filters: dict, batch_size: int):
    iterator = await asyncio.to_thread(
        lambda: iter(
            adapter.iter_session_id_batches(
                filters,
                batch_size=batch_size,
            )
        )
    )
    while True:
        batch = await asyncio.to_thread(_next_batch, iterator)
        if batch is None:
            return
        if (
            isinstance(batch, (str, bytes))
            or not isinstance(batch, (list, tuple))
        ):
            raise AdapterError(
                "iter_session_id_batches() must yield lists of Session IDs"
            )
        if batch:
            yield list(batch)


async def pull_sessions(
    config,
    tenant,
    ingest,
    body: dict,
    *,
    progress=None,
) -> dict:
    descriptor = describe(config, tenant)
    request = dict(body)
    request.pop("max_sessions", None)
    filters, _preview_limit = filters_for(descriptor, request)
    adapter = await asyncio.to_thread(
        load, config, tenant, revision=descriptor.get("revision"),
    )
    concurrency = _positive_env(
        "TEAMEVOLVER_DATASOURCE_PULL_CONCURRENCY",
        8,
        maximum=64,
    )
    batch_size = _positive_env(
        "TEAMEVOLVER_DATASOURCE_PULL_BATCH_SIZE",
        100,
        maximum=1000,
    )
    result_limit = _positive_env(
        "TEAMEVOLVER_DATASOURCE_PULL_RESULT_LIMIT",
        200,
        maximum=5000,
    )
    semaphore = asyncio.Semaphore(concurrency)
    counts = {
        key: 0
        for key in ("queued", "skipped", "duplicate", "empty", "error", "filtered")
    }
    results: list[dict[str, Any]] = []
    total = 0
    retry_count = 0
    try:
        async def process(sid):
            async with semaphore:
                try:
                    raw, traces = await asyncio.to_thread(adapter.fetch_session, sid)
                    session = await asyncio.to_thread(adapter.convert_session, raw, traces)
                    if not isinstance(session, dict) or not isinstance(session.get("turns"), list):
                        raise AdapterError("Adapter must return a Session with turns")
                    if not any(isinstance(t, dict) and (
                        str(t.get("prompt_text") or "").strip()
                        or str(t.get("response_text") or "").strip()
                        or t.get("tool_calls") or t.get("tool_results")
                    ) for t in session["turns"]):
                        return {"session_id": sid, "status": "empty"}
                    original = str(session.get("session_id") or sid)
                    try:
                        session["session_id"] = sanitize_session_id(
                            original,
                            fallback=None,
                        )
                    except InvalidSessionId as exc:
                        raise AdapterError(
                            "Adapter returned an invalid session_id"
                        ) from exc
                    session.setdefault("metadata", {}).update(
                        upstream_session_id=original, adapter_file=descriptor["file"],
                        adapter_revision=descriptor["revision"],
                    )
                    session.setdefault("user_alias", str(session.get("user_id") or "upstream"))
                    session["force_reprocess"] = body.get("force_reprocess", False)
                    session["defer_evolution_trigger"] = body.get("defer_evolution_trigger", False)
                    if session["force_reprocess"]:
                        session["reprocess_reason"] = "tenant adapter pull"
                    outcome = await ingest(session)
                    return {"session_id": session["session_id"], "status": outcome.get("status", "unknown"),
                            "turns": len(session["turns"]), "queued": bool(outcome.get("queued")),
                            "value_judge": outcome.get("value_judge")}
                except Exception as exc:
                    return {"session_id": sid, "status": "error", "reason": str(exc)}

        async for raw_batch in _session_id_batches(
            adapter,
            filters,
            batch_size,
        ):
            ids = list(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in raw_batch
                    if str(value or "").strip()
                )
            )
            if not ids:
                continue

            included_ids, excluded = apply_session_id_exclusions(
                descriptor,
                filters,
                ids,
            )
            processed = await asyncio.gather(
                *(process(sid) for sid in included_ids)
            )
            processed_by_id = dict(zip(included_ids, processed))
            excluded_by_id = {item["session_id"]: item for item in excluded}
            for sid in ids:
                item = excluded_by_id.get(sid) or processed_by_id[sid]
                total += 1
                status = str(item.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
                if len(results) < result_limit:
                    results.append(item)
            if progress is not None:
                update = progress(
                    {
                        "total": total,
                        "counts": dict(counts),
                    }
                )
                if inspect.isawaitable(update):
                    await update

        retry_count = getattr(adapter, "retry_count", 0)
        if callable(retry_count):
            retry_count = retry_count()
    finally:
        await asyncio.to_thread(adapter.close)
    return {
        "filters": filters,
        "total": total,
        "counts": counts,
        "retry_count": int(retry_count or 0),
        "results": results,
        "results_truncated": total > len(results),
        "result_limit": result_limit,
    }
