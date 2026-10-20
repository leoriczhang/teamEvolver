"""Durable Session pull, semantic split, analysis and archive handlers.

Every task uses the accepted payload and fixed adapter/configuration revisions.
Mandatory analysis and trace analysis must succeed before archive creates a
formal Session. Failed attempts retry under Worker fencing; completed stages
survive process restarts. API roles only persist acceptance and never run these
handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from teamEvolver.runtime import HandlerResult
from teamEvolver.runtime.repository import ClaimedTask

from ..adapters.repository import AdapterRepository
from ..projections import session_list_meta
from ..repository import SessionIngestionRepository
from ..schemas import (
    MANDATORY_STAGES,
    PAYLOAD_SCHEMA_VERSION,
    IngestionTaskPayload,
    session_payload_hash,
)

logger = logging.getLogger(__name__)

# Task kinds this module owns.
KIND_PULL = "session.pull"
KIND_NORMALIZE = "session.normalize"
KIND_SPLIT = "session.split"
KIND_ANALYZE = "session.analyze"
KIND_ARCHIVE = "session.archive"

_SPLIT_PLAN_SCHEMA = "session-split-plan.v1"


class SessionIngestionDeps:
    """Collaborators a worker role injects via ``services.extras['session_ingestion']``.

    - ``payloads`` — raw-payload store: ``get(ref) -> dict`` loads a pinned
      session envelope; ``put(ingestion_id, session) -> (ref, hash)`` persists one.
    - ``stage_runner`` — normalization, semantic split and LLM-backed analysis.
    - ``adapter_source`` — ``load(adapter_id, revision) -> adapter`` returning a
      pull adapter pinned to an immutable revision.
    """

    def __init__(self, *, payloads: Any, stage_runner: Any, adapter_source: Any = None) -> None:
        self.payloads = payloads
        self.stage_runner = stage_runner
        self.adapter_source = adapter_source


def _deps(services) -> SessionIngestionDeps:
    extras = getattr(services, "extras", None) or {}
    deps = extras.get("session_ingestion")
    if not isinstance(deps, SessionIngestionDeps):
        raise RuntimeError(
            "Session Ingestion handlers: services.extras['session_ingestion'] must be a SessionIngestionDeps "
            "(the worker role injects it; the api role never runs these handlers)"
        )
    return deps


async def _enqueue_next(services, task: ClaimedTask, *, kind: str, payload: IngestionTaskPayload) -> None:
    """Enqueue the next stage task for this ingestion, in its own short tx."""
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.enqueue(
            conn,
            tenant_id=task.tenant_id,
            task_id=f"{payload.ingestion_id}:{kind}",
            kind=kind,
            subject_type="ingestion",
            subject_id=payload.ingestion_id,
            schema_version=PAYLOAD_SCHEMA_VERSION,
            dedupe_key=f"{payload.ingestion_id}:{kind}",
            payload_ref=task.payload_ref,
            payload_hash=task.payload_hash,
            config_revision=payload.config_revision,
            adapter_revision=payload.adapter_revision,
            trace_id=task.trace_id,
        )


async def _load_payload(deps: SessionIngestionDeps, task: ClaimedTask) -> IngestionTaskPayload:
    return IngestionTaskPayload.from_json(await asyncio.to_thread(deps.payloads.get_payload_json, task.payload_ref))


# --------------------------------------------------------------------------- #
# session.normalize
# --------------------------------------------------------------------------- #


async def handle_normalize(task: ClaimedTask, services) -> HandlerResult:
    """Normalize the pinned raw session; record the ``normalize`` stage.

    On success enqueues ``session.split``; on a transient failure returns a
    retry so the worker loop re-attempts under its fence.
    """
    deps = _deps(services)
    repo = SessionIngestionRepository(services.tx)
    payload = await _load_payload(deps, task)
    session = await asyncio.to_thread(deps.payloads.get_session, task.payload_ref)
    try:
        normalized = await deps.stage_runner.normalize(session, payload=payload)
    except Exception as exc:  # noqa: BLE001 - transient upstream/model errors retry
        await _record_failed(repo, task, "normalize", exc)
        return HandlerResult.retry(error_code=f"normalize_failed:{type(exc).__name__}")
    ref, _ = await asyncio.to_thread(deps.payloads.put_session, payload.ingestion_id, normalized, stage="normalize")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await repo.record_stage(
            conn, tenant_id=task.tenant_id, ingestion_id=payload.ingestion_id,
            stage="normalize", status="succeeded", result_ref=ref,
        )
        await repo.set_status(conn, ingestion_id=payload.ingestion_id, status="processing")
    await _enqueue_next(services, task, kind=KIND_SPLIT, payload=payload)
    return HandlerResult.succeed(result_ref=ref)


# --------------------------------------------------------------------------- #
# session.split
# --------------------------------------------------------------------------- #


def _validated_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("semantic split must return at least one Session")
    segments: list[dict[str, Any]] = []
    for segment in value:
        if not isinstance(segment, dict):
            raise ValueError("semantic split returned a non-object Session")
        if not str(segment.get("session_id") or "").strip():
            raise ValueError("semantic split Session requires session_id")
        turns = segment.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("semantic split Session requires turns")
        segments.append(segment)
    return segments


async def _load_or_create_split_plan(deps, repo, tx, task, payload):
    async with tx.tenant_tx(task.tenant_id) as conn:
        rows = await repo.stage_results(conn, ingestion_id=payload.ingestion_id)
    existing = next(
        (row for row in rows if row["stage"] == "split" and row.get("result_ref")),
        None,
    )
    if existing is not None:
        document = await asyncio.to_thread(deps.payloads.get_session, existing["result_ref"])
        if isinstance(document, dict) and document.get("schema_version") == _SPLIT_PLAN_SCHEMA:
            return _validated_segments(document.get("segments")), existing["result_ref"]

    session = await asyncio.to_thread(deps.payloads.get_session, task.payload_ref)
    segments = _validated_segments(await deps.stage_runner.split(session, payload=payload))
    document = {"schema_version": _SPLIT_PLAN_SCHEMA, "segments": segments}
    ref, _ = await asyncio.to_thread(
        deps.payloads.put_session,
        payload.ingestion_id,
        document,
        stage="split-plan",
    )
    async with tx.tenant_tx(task.tenant_id) as conn:
        await repo.record_stage(
            conn,
            tenant_id=task.tenant_id,
            ingestion_id=payload.ingestion_id,
            stage="split",
            status="pending",
            result_ref=ref,
            detail={"segments": len(segments)},
        )
    return segments, ref


async def handle_split(task: ClaimedTask, services) -> HandlerResult:
    """Persist one semantic split plan, then continue or fan out child Sessions.

    The split plan is immutable and checkpointed before child acceptance. A
    retry therefore reuses the same semantic boundaries and deterministic child
    ingestion IDs instead of asking the model to decide boundaries again.
    """
    deps = _deps(services)
    repo = SessionIngestionRepository(services.tx)
    payload = await _load_payload(deps, task)
    try:
        segments, plan_ref = await _load_or_create_split_plan(
            deps,
            repo,
            services.tx,
            task,
            payload,
        )
    except Exception as exc:  # noqa: BLE001 - model/storage failures retry
        await _record_failed(repo, task, "split", exc)
        return HandlerResult.retry(error_code=f"split_failed:{type(exc).__name__}")

    if len(segments) == 1:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await repo.record_stage(
                conn,
                tenant_id=task.tenant_id,
                ingestion_id=payload.ingestion_id,
                stage="split",
                status="succeeded",
                result_ref=plan_ref,
                detail={"segments": 1, "split": False},
            )
            await repo.set_status(conn, ingestion_id=payload.ingestion_id, status="processing")
        await _enqueue_next(services, task, kind=KIND_ANALYZE, payload=payload)
        return HandlerResult.succeed(result_ref=plan_ref)

    child_ids: list[str] = []
    try:
        for index, segment in enumerate(segments, 1):
            child_id = f"{payload.ingestion_id}:split:{index:03d}"
            child_ids.append(child_id)
            child_payload = IngestionTaskPayload(
                schema_version=PAYLOAD_SCHEMA_VERSION,
                source=payload.source,
                ingestion_id=child_id,
                tenant_id=task.tenant_id,
                external_id=str(segment["session_id"]),
                user_id=payload.user_id,
                account_id=payload.account_id,
                config_revision=payload.config_revision,
                adapter_id=payload.adapter_id,
                adapter_revision=payload.adapter_revision,
                payload_ref=f"ingest/{task.tenant_id}/{child_id.replace(':', '-')}.json",
                payload_hash=session_payload_hash(segment),
            )
            await _accept_and_enqueue_child(
                services,
                repo,
                task,
                child_payload,
                segment,
                deps,
            )
    except Exception as exc:  # noqa: BLE001 - preserve the plan across retries
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await repo.record_stage(
                conn,
                tenant_id=task.tenant_id,
                ingestion_id=payload.ingestion_id,
                stage="split",
                status="pending",
                result_ref=plan_ref,
                detail={"segments": len(segments), "children": child_ids},
                error_code=f"{type(exc).__name__}",
            )
        return HandlerResult.retry(error_code=f"split_fanout_failed:{type(exc).__name__}")

    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await repo.record_stage(
            conn,
            tenant_id=task.tenant_id,
            ingestion_id=payload.ingestion_id,
            stage="split",
            status="succeeded",
            result_ref=plan_ref,
            detail={"segments": len(segments), "children": child_ids, "split": True},
        )
        await repo.set_status(conn, ingestion_id=payload.ingestion_id, status="skipped")
    return HandlerResult.succeed(result_ref=plan_ref)


# --------------------------------------------------------------------------- #
# session.analyze (merged Session Analyze + per-trace Trace Analyze)
# --------------------------------------------------------------------------- #


async def handle_analyze(task: ClaimedTask, services) -> HandlerResult:
    """Run the two mandatory model stages: Session Analyze then Trace Analyze.

    Both must succeed before the Session can be archived.
    Either model stage failing records a failed stage row and retries; neither
    writes a formal Session or Evidence.
    """
    deps = _deps(services)
    repo = SessionIngestionRepository(services.tx)
    payload = await _load_payload(deps, task)
    session = await asyncio.to_thread(deps.payloads.get_session, task.payload_ref)

    try:
        analysis = await deps.stage_runner.analyze(session, payload=payload)
    except Exception as exc:  # noqa: BLE001 - model errors retry; no partial Session
        await _record_failed(repo, task, "analyze", exc)
        return HandlerResult.retry(error_code=f"analyze_failed:{type(exc).__name__}")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await repo.record_stage(
            conn, tenant_id=task.tenant_id, ingestion_id=payload.ingestion_id,
            stage="analyze", status="succeeded",
            detail={"decision": analysis.get("decision", "")},
        )

    try:
        await deps.stage_runner.trace_analyze(session, payload=payload)
    except Exception as exc:  # noqa: BLE001 - trace model errors retry
        await _record_failed(repo, task, "trace_analyze", exc)
        return HandlerResult.retry(error_code=f"trace_analyze_failed:{type(exc).__name__}")
    ref, _ = await asyncio.to_thread(deps.payloads.put_session, payload.ingestion_id, session, stage="analyze")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await repo.record_stage(
            conn, tenant_id=task.tenant_id, ingestion_id=payload.ingestion_id,
            stage="trace_analyze", status="succeeded", result_ref=ref,
        )
    await _enqueue_next(services, task, kind=KIND_ARCHIVE, payload=payload)
    return HandlerResult.succeed(result_ref=ref)


# --------------------------------------------------------------------------- #
# session.archive — writes the formal Session only after all mandatory stages
# --------------------------------------------------------------------------- #


async def handle_archive(task: ClaimedTask, services) -> HandlerResult:
    """Write the formal Session atomically, gated on all mandatory stages.

    Verifies every stage in :data:`MANDATORY_STAGES` except ``archive`` has a
    ``succeeded`` row; if any is missing the task fails (no formal Session). On
    success it writes the Session (business + ingest time), the archive stage row
    and the ingestion status in one short transaction.
    """
    deps = _deps(services)
    repo = SessionIngestionRepository(services.tx)
    payload = await _load_payload(deps, task)
    session = await asyncio.to_thread(deps.payloads.get_session, task.payload_ref)

    required = set(MANDATORY_STAGES) - {"archive"}
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        done = await repo.succeeded_stages(conn, ingestion_id=payload.ingestion_id)
    missing = sorted(required - done)
    if missing:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await repo.record_stage(
                conn, tenant_id=task.tenant_id, ingestion_id=payload.ingestion_id,
                stage="archive", status="failed",
                error_code="mandatory_stages_incomplete",
                detail={"missing": missing},
            )
            await repo.set_status(conn, ingestion_id=payload.ingestion_id, status="failed")
        return HandlerResult.fail(error_code=f"mandatory_stages_incomplete:{','.join(missing)}")

    try:
        archived = await deps.stage_runner.archive(session, payload=payload)
    except Exception as exc:  # noqa: BLE001
        await _record_failed(repo, task, "archive", exc)
        return HandlerResult.retry(error_code=f"archive_failed:{type(exc).__name__}")

    decision = str(archived.get("decision") or "")
    archived_session = archived.get("session") or session
    content_ref, content_hash = await asyncio.to_thread(deps.payloads.put_session, 
        payload.ingestion_id, archived_session, stage="archive"
    )
    final_status = "skipped" if decision == "skipped" else "archived"
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        if decision == "valuable" or (decision == "skipped" and archived.get("persist_skipped", True)):
            await repo.write_session(
                conn,
                tenant_id=task.tenant_id,
                session_id=f"session_{payload.ingestion_id}",
                ingestion_id=payload.ingestion_id,
                source=payload.source,
                external_id=payload.external_id,
                user_id=payload.user_id,
                account_id=payload.account_id,
                decision=decision,
                content_ref=content_ref,
                content_hash=content_hash,
                judge=archived.get("judge") or {},
                filter_audit=archived.get("filter_audit") or {},
                consumption=archived.get("consumption") or {},
                meta=session_list_meta(archived_session, archived.get("meta")),
                business_time=archived.get("business_time"),
            )
        await repo.record_stage(
            conn, tenant_id=task.tenant_id, ingestion_id=payload.ingestion_id,
            stage="archive", status="succeeded", result_ref=content_ref,
            detail={"decision": decision},
        )
        await repo.set_status(conn, ingestion_id=payload.ingestion_id, status=final_status)
    return HandlerResult.succeed(result_ref=content_ref)


# --------------------------------------------------------------------------- #
# session.pull — bounded, resumable fan-out into per-session normalize tasks
# --------------------------------------------------------------------------- #


async def handle_pull(task: ClaimedTask, services) -> HandlerResult:
    """Pull bounded upstream sessions and enqueue per-session normalization.

    The accepted adapter revision remains fixed. Filtering runs before remote
    fetch; per-item upstream errors are recorded without failing the batch."""
    deps = _deps(services)
    if deps.adapter_source is None:
        return HandlerResult.fail(error_code="pull_adapter_source_missing")
    repo = SessionIngestionRepository(services.tx)
    adapter_repo = AdapterRepository(services.tx)
    payload = await _load_payload(deps, task)

    async with services.tx.tenant_tx(task.tenant_id) as conn:
        version = await adapter_repo.get_version(
            conn, adapter_id=payload.adapter_id, revision=payload.adapter_revision
        )
    if version is None:
        return HandlerResult.fail(error_code="pull_adapter_revision_not_found")

    operation = payload.pull.get("operation", "pull")
    if operation != "pull":

        from ..adapters.testing import execute
        result = await asyncio.to_thread(execute, version["source"], payload.adapter_id, payload.pull)
        ref, _ = await asyncio.to_thread(deps.payloads.put_session, payload.ingestion_id, result, stage="adapter-test")
        return HandlerResult.succeed(result_ref=ref)
    adapter = deps.adapter_source.load(
        adapter_id=payload.adapter_id, revision=payload.adapter_revision, source=version["source"]
    )
    counts = {"fetched": 0, "enqueued": 0, "empty": 0, "error": 0, "duplicate": 0}
    try:
        filters = dict(payload.pull.get("filters", {}))
        filters.update({k: payload.pull[k] for k in ("from_timestamp", "to_timestamp") if k in payload.pull})
        async for item in adapter.iter_sessions(filters):
            counts["fetched"] += 1
            if item.get("status") == "error":
                counts["error"] += 1
                continue
            session = item.get("session")
            if not session:
                counts["empty"] += 1
                continue
            child_id = f"{payload.ingestion_id}:{item['external_id']}"
            child_payload = IngestionTaskPayload(
                schema_version=PAYLOAD_SCHEMA_VERSION,
                source=payload.source,
                ingestion_id=child_id,
                tenant_id=task.tenant_id,
                external_id=str(item["external_id"]),
                user_id=str(session.get("user_id") or ""),
                account_id=payload.account_id,
                config_revision=payload.config_revision,
                adapter_id=payload.adapter_id,
                adapter_revision=payload.adapter_revision,
            )
            ref, digest = await asyncio.to_thread(deps.payloads.put_session, child_id, session, stage="raw")
            child_payload.payload_ref = f"ingest/{task.tenant_id}/{child_id.replace(':', '-')}.json"
            child_payload.payload_hash = digest
            enqueued = await _accept_and_enqueue_child(
                services, repo, task, child_payload, session, deps
            )
            counts["enqueued" if enqueued else "duplicate"] += 1
    except Exception as exc:  # noqa: BLE001 - a hard pull failure retries the whole task
        logger.warning("[session.pull] tenant=%s pull failed: %s", task.tenant_id, exc)
        return HandlerResult.retry(error_code=f"pull_failed:{type(exc).__name__}")
    logger.info("[session.pull] tenant=%s counts=%s", task.tenant_id, counts)
    ref, _ = await asyncio.to_thread(deps.payloads.put_session, payload.ingestion_id, counts, stage="pull-result")
    return HandlerResult.succeed(result_ref=ref)


async def _accept_and_enqueue_child(services, repo, task, child_payload, session, deps) -> bool:
    """Accept one pulled session (ingestion + payload) and enqueue normalize.

    Returns ``True`` when a new normalize task was enqueued, ``False`` when the
    item deduped against an existing live ingestion.
    """
    payload_json = child_payload.to_json()
    await asyncio.to_thread(deps.payloads.put_payload_json, child_payload.payload_ref, payload_json, session)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        accepted_id = await repo.accept(
            conn,
            tenant_id=task.tenant_id,
            ingestion_id=child_payload.ingestion_id,
            source=child_payload.source,
            external_id=child_payload.external_id,
            user_id=child_payload.user_id,
            account_id=child_payload.account_id,
            dedupe_key=child_payload.ingestion_id,
            task_id=f"{child_payload.ingestion_id}:{KIND_NORMALIZE}",
            adapter_id=child_payload.adapter_id,
            adapter_revision=child_payload.adapter_revision,
            config_revision=child_payload.config_revision,
            payload_ref=child_payload.payload_ref,
            payload_hash=child_payload.payload_hash,
            business_time=session.get("business_time"),
        )
        if accepted_id != child_payload.ingestion_id:
            return False
        await services.tasks.enqueue(
            conn, tenant_id=task.tenant_id, task_id=f"{child_payload.ingestion_id}:{KIND_NORMALIZE}",
            kind=KIND_NORMALIZE, subject_type="ingestion", subject_id=child_payload.ingestion_id,
            schema_version=PAYLOAD_SCHEMA_VERSION, dedupe_key=f"{child_payload.ingestion_id}:{KIND_NORMALIZE}",
            payload_ref=child_payload.payload_ref, payload_hash=child_payload.payload_hash,
            config_revision=child_payload.config_revision, adapter_revision=child_payload.adapter_revision,
        )
    return True




async def _record_failed(repo: SessionIngestionRepository, task: ClaimedTask, stage: str, exc: Exception) -> None:
    payload = None
    try:
        async with repo._tx.tenant_tx(task.tenant_id) as conn:  # noqa: SLF001 - same module helper
            await repo.record_stage(
                conn, tenant_id=task.tenant_id, ingestion_id=task.subject_id,
                stage=stage, status="failed", error_code=f"{type(exc).__name__}",
            )
    except Exception:  # noqa: BLE001 - never let bookkeeping mask the original failure
        logger.warning("[session.%s] failed to record stage failure for %s", stage, task.subject_id)
    del payload


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

HANDLERS = {
    KIND_PULL: handle_pull,
    KIND_NORMALIZE: handle_normalize,
    KIND_SPLIT: handle_split,
    KIND_ANALYZE: handle_analyze,
    KIND_ARCHIVE: handle_archive,
}


def register_handlers() -> dict[str, Any]:
    """Return this module's task-kind → handler map for the worker registry.

    Called by the worker-role composition seam; the returned map is merged into
    the fixed, validated ``TaskRegistry``.
    """
    return dict(HANDLERS)


__all__ = [
    "SessionIngestionDeps",
    "KIND_PULL",
    "KIND_NORMALIZE",
    "KIND_SPLIT",
    "KIND_ANALYZE",
    "KIND_ARCHIVE",
    "HANDLERS",
    "register_handlers",
    "handle_pull",
    "handle_normalize",
    "handle_split",
    "handle_analyze",
    "handle_archive",
]
