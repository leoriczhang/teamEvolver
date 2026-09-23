"""Bounded ingestion of standard Sessions produced by a tenant adapter."""

import asyncio

from session_ingestion.adapters._runtime import (
    AdapterError,
    apply_session_id_exclusions,
    describe,
    filters_for,
    load,
)
from session_ingestion.identifiers import InvalidSessionId, sanitize_session_id


async def pull_sessions(config, tenant, ingest, body: dict) -> dict:
    descriptor = describe(config, tenant)
    filters, cap = filters_for(descriptor, body)
    adapter = await asyncio.to_thread(
        load, config, tenant, revision=descriptor.get("revision"),
    )
    semaphore = asyncio.Semaphore(8)
    try:
        ids = await asyncio.to_thread(adapter.list_session_ids, filters, max_sessions=cap)
        if not isinstance(ids, list) or len(ids) > cap:
            raise AdapterError("Adapter exceeded the session limit")
        ids = list(dict.fromkeys(ids))
        included_ids, excluded = apply_session_id_exclusions(
            descriptor,
            filters,
            ids,
        )

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

        processed = await asyncio.gather(*(process(sid) for sid in included_ids))
        processed_by_id = dict(zip(included_ids, processed))
        excluded_by_id = {item["session_id"]: item for item in excluded}
        results = [
            excluded_by_id.get(sid) or processed_by_id[sid]
            for sid in ids
        ]
        retry_count = getattr(adapter, "retry_count", 0)
        if callable(retry_count):
            retry_count = retry_count()
    finally:
        await asyncio.to_thread(adapter.close)
    counts = {key: 0 for key in ("queued", "skipped", "duplicate", "empty", "error", "filtered")}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "filters": filters,
        "total": len(ids),
        "counts": counts,
        "retry_count": int(retry_count or 0),
        "results": results,
    }
