"""Shared Session ingestion pipeline, independent of the inbound transport."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from team_skills.evolution.session_filter import SessionValueClassifier
from teamEvolver.session_store import SessionStore
from teamEvolver.tenants.registry import (
    current_tenant_id,
    effective_config,
    get_current_tenant,
)

logger = logging.getLogger(__name__)


class SessionIngestionUnavailable(RuntimeError):
    """Raised when the tenant's Session store cannot be initialized."""


def _public_judge_from_scores(scores: dict[str, Any]) -> dict[str, Any]:
    """Public ``judge`` dict (same shape as the async post-ingest judge).

    Lets a merged-analysis session surface its Good/Bad review in the console
    even when it is skipped and never enters an evolution cycle.
    """
    payload: dict[str, Any] = {
        "overall_score": scores.get("overall_score"),
        "rationale": str(scores.get("rationale") or ""),
        "reasons": scores.get("reasons") or {},
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "judge_source": "merged_ingest",
    }
    for dim in ("task_completion", "response_quality", "efficiency", "tool_usage"):
        value = scores.get(dim)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            payload[dim] = float(value)
    evidence = str(scores.get("evolution_evidence") or "").strip().lower()
    if evidence in {"defect", "exemplary", "none"}:
        payload["evolution_evidence"] = evidence
        reason = str(scores.get("evidence_reason") or "").strip()
        if reason and evidence != "none":
            payload["evidence_reason"] = reason
    if "skill_experiences" in scores:
        payload["skill_experiences"] = scores["skill_experiences"]
    return payload


async def _classify_session(
    session: dict[str, Any],
    classifier: SessionValueClassifier,
) -> dict[str, Any]:
    """Run the mandatory merged Session Analyze stage.

    The merged ``analyze_session`` produces the value_judge decision, the
    ``_summary`` text and the ``_judge_scores`` in a single LLM call. Every
    non-empty Session must complete this stage before it can be archived or
    queued; no deterministic audit, feature switch, or heuristic fallback may
    bypass it.
    """
    from team_skills.evolution.stages.analyze import (
        SessionAnalysisError,
        analyze_session,
    )

    if classifier.client is None:
        raise SessionAnalysisError(
            "Session Analyze requires a configured LLM client"
        )

    value_judge = await analyze_session(classifier.client, session)
    scores = session["_judge_scores"]
    session["judge"] = _public_judge_from_scores(scores)
    return value_judge


async def _analyze_traces(
    session: dict[str, Any],
    classifier: SessionValueClassifier,
) -> None:
    """Analyze every trace before the session is archived or queued.

    Runs after ``_classify_session`` has stamped ``_judge_scores``. It emits one
    result per trace and fires the registered hook once per result (see
    ``stages/trace_analyze``). Model and result validation failures propagate
    so ingest cannot continue without complete model analysis.
    """
    from team_skills.evolution.stages.trace_analyze import analyze_and_dispatch_traces

    await analyze_and_dispatch_traces(classifier.client, session)


def _effective_config(owner: Any) -> Any:
    try:
        tenant = get_current_tenant()
    except Exception:  # noqa: BLE001 - background/default context
        return owner.config
    return effective_config(None, tenant, owner.config)


_STATUS_PRIORITY = ("queued", "skipped", "duplicate", "filtered", "empty", "error", "unknown")


def _aggregate_split_outcome(
    original_session_id: str,
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold per-segment ingest outcomes into one response for the caller.

    Callers (push route, pull orchestration) read ``status`` / ``queued`` /
    ``value_judge``; the per-segment detail stays in ``segments``.
    """
    statuses = [str(outcome.get("status") or "unknown") for outcome in outcomes]
    status = next((s for s in _STATUS_PRIORITY if s in statuses), "unknown")
    return {
        "status": status,
        "session_id": original_session_id,
        "queued": any(outcome.get("queued") for outcome in outcomes),
        "split": True,
        "segments": [
            {"session_id": outcome.get("session_id"), "status": outcome.get("status")}
            for outcome in outcomes
        ],
        "value_judge": next(
            (
                outcome.get("value_judge")
                for outcome in outcomes
                if outcome.get("value_judge")
            ),
            None,
        ),
    }


async def ingest(
    owner: Any,
    session: dict[str, Any],
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Split oversized mixed-topic sessions, then classify/queue each part.

    A session that spans many turns may mix several unrelated topics or
    chitchat; :func:`session_ingestion.split.split_session` cuts it into
    topic-coherent sub-sessions before classification so each part gets its
    own value verdict. Single-segment results behave exactly like before.
    """
    config = _effective_config(owner)
    try:
        from .split import split_session

        segments = await split_session(config, session)
    except Exception as exc:  # noqa: BLE001 - splitting must never block ingest
        logger.warning(
            "[SessionSplit] split failed for %s; ingesting unsplit: %s",
            session.get("session_id"),
            exc,
        )
        segments = [session]
    if len(segments) <= 1:
        return await _ingest_one(owner, session, invalidate_cache=invalidate_cache)

    original_id = str(session.get("session_id") or "")
    outcomes: list[dict[str, Any]] = []
    for segment in segments:
        try:
            outcomes.append(
                await _ingest_one(owner, segment, invalidate_cache=invalidate_cache)
            )
        except SessionIngestionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad segment must not fail the rest
            logger.warning(
                "[SessionSplit] segment ingest failed for %s: %s",
                segment.get("session_id"),
                exc,
            )
            outcomes.append(
                {
                    "status": "error",
                    "session_id": str(segment.get("session_id") or ""),
                    "queued": False,
                }
            )
    return _aggregate_split_outcome(original_id, outcomes)


async def _ingest_one(
    owner: Any,
    session: dict[str, Any],
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Classify, archive, queue, and schedule one normalized Session."""
    session_id = str(session.get("session_id") or "")
    config = _effective_config(owner)
    try:
        session_store = await asyncio.to_thread(
            SessionStore.from_config,
            config,
            current_tenant_id(),
        )
    except Exception as exc:  # noqa: BLE001
        raise SessionIngestionUnavailable(
            "session storage is not configured"
        ) from exc

    force_reprocess = bool(session.pop("force_reprocess", False))
    if not force_reprocess and await asyncio.to_thread(
        session_store.duplicate_of_processed,
        session,
    ):
        logger.info(
            "[SessionFilter] skipped duplicate session=%s "
            "(already processed, no new content)",
            session_id,
        )
        return {"status": "duplicate", "session_id": session_id, "queued": False}
    if force_reprocess:
        session["reprocess_reason"] = str(
            session.get("reprocess_reason") or "explicit dashboard reingest"
        )

    classifier = SessionValueClassifier.from_config(config)
    value_judge = await _classify_session(session, classifier)
    session["value_judge"] = value_judge
    session["ingested_at"] = datetime.now(timezone.utc).isoformat()

    # Both model stages must succeed before the Session is archived or queued.
    await _analyze_traces(session, classifier)

    if value_judge.get("decision") != "valuable":
        await asyncio.to_thread(session_store.save_skipped, session)
        if invalidate_cache is not None:
            invalidate_cache(f"conversations:{id(owner.config)}", f"skill-experiences:{id(owner.config)}")
        logger.info(
            "[SessionFilter] skipped session=%s decision=%s reason=%s",
            session_id,
            value_judge.get("decision"),
            value_judge.get("reason"),
        )
        return {
            "status": "skipped",
            "session_id": session_id,
            "queued": False,
            "value_judge": value_judge,
        }

    key = await asyncio.to_thread(session_store.save_queued, session)
    if invalidate_cache is not None:
        invalidate_cache(
            f"queue:{id(owner.config)}",
            f"conversations:{id(owner.config)}",
            f"status:{id(owner.config)}",
            f"skill-experiences:{id(owner.config)}",
        )
    trigger_scheduled = (
        False
        if bool(session.get("defer_evolution_trigger"))
        else owner._schedule_evolve_trigger()
    )
    logger.info("[SessionFilter] queued valuable session=%s key=%s", session_id, key)
    return {
        "status": "queued",
        "session_id": session_id,
        "queued": True,
        "key": key,
        "trigger_scheduled": trigger_scheduled,
        "value_judge": value_judge,
    }
