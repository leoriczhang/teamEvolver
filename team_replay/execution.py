"""System-owned Replay loop, independent of runtime transport and registration."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Mapping

from ._util import stable_hash
from .execution_inputs import plan_input_messages
from .execution_plan import execution_plan_hash, require_execution_capabilities
from .hooks import (
    RUNTIME_METRICS,
    ReplayAdapterFactory,
    ReplayContext,
    ReplaySession,
    ReplayUnsupported,
    validate_observation,
)
from .judging import judge_checklist, render_user_feedback
from .metrics import UNAVAILABLE, count_tool_calls
from .policy import normalize_case_checklist, progressive_config, select_disclosure_items
from .request_capture import verify_execution_receipt


def run_branch(
    factory: ReplayAdapterFactory, context: ReplayContext, case: Mapping[str, Any],
    *, harness: Mapping[str, Any], max_interactions: int,
    judge: Callable = judge_checklist, feedback: Callable = render_user_feedback,
    session: ReplaySession | None = None, checklist_required: bool = True,
) -> dict[str, Any]:
    """Only open(context), send(message) and close() touch the customer runtime."""
    started = time.monotonic()
    query = str(case.get("query") or "")
    checklist_enabled = checklist_required or max_interactions > 1
    checklist = normalize_case_checklist(case) if checklist_enabled else []
    result: dict[str, Any] = {
        "branch": context.treatment.branch, "runtime": context.runtime_type,
        "request_id": context.request_id, "ok": False, "completed": False,
        "context_input_hash": stable_hash(context.context_snapshot),
        "execution_manifest_hash": stable_hash({
            "runtime_type": context.runtime_type, "materials": context.materials,
            "timeout_seconds": context.timeout_seconds, "query": query,
            "max_interactions": max_interactions if checklist_enabled else 1,
            **({"checklist_required": False} if not checklist_required else {}),
        }),
        "messages": plan_input_messages(context.execution_plan),
        "interactions": [], "artifacts": [], "disclosures": [],
        "trace_ids": [], "metrics_incomplete_reasons": [],
        "execution_plan_hash": execution_plan_hash(context.execution_plan) if context.execution_plan else "",
        "execution_receipts": [], "observed_requests": [],
        "execution_verification": "pending" if context.execution_plan is not None else "not_required",
        "checklist_report": ({"judge": "unavailable", "all_satisfied": False} if checklist_enabled else {
            "judge": "not_run", "all_satisfied": False, "total": 0, "items": [],
            "reason": "external_evaluation_selected",
        }),
    }
    totals: dict[str, int | float] = {}
    unavailable: set[str] = set()
    current_message = query
    disclosed_ids: set[str] = set()
    disclosed_texts: list[str] = []
    batch_size = progressive_config(case)["batch_size"] if checklist_enabled else 0
    try:
        if not query.strip() or (checklist_enabled and not checklist):
            raise ValueError("Replay requires an initial query and a nonempty Checklist")
        require_execution_capabilities(factory, context.execution_plan)
        if session is None:
            session = factory.open(copy.deepcopy(context))
        if not callable(getattr(session, "send", None)) or not callable(getattr(session, "close", None)):
            raise TypeError("factory.open(context) must return a ReplaySession with send() and close()")
        interaction_limit = max(1, min(20, max_interactions)) if checklist_enabled else 1
        for turn in range(1, interaction_limit + 1):
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            cursor = len(context.request_observer.records()) if context.request_observer is not None else 0
            observation = validate_observation(session.send(current_message))
            if context.execution_plan is not None:
                observed = verify_execution_receipt(
                    context, observation.execution_receipt, turn_num=turn, user_message=current_message, after=cursor,
                )
                result["observed_requests"].extend(observed)
                result["execution_receipts"].append(copy.deepcopy(dict(observation.execution_receipt)))
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            round_metrics = dict(observation.metrics or {})
            if any("tool_calls" in message for message in observation.messages):
                round_metrics["tool_call_count"] = count_tool_calls(list(observation.messages))
            for key in RUNTIME_METRICS:
                if key not in round_metrics:
                    unavailable.add(key)
                else:
                    totals[key] = totals.get(key, 0) + round_metrics[key]
            result["messages"].extend(copy.deepcopy(list(observation.messages)))
            result["artifacts"].extend(copy.deepcopy(list(observation.artifacts)))
            if observation.trace_id:
                result["trace_ids"].append(observation.trace_id)
            if observation.metrics_incomplete_reason:
                result["metrics_incomplete_reasons"].append(
                    observation.metrics_incomplete_reason
                )
            interaction = {
                "interaction_num": turn, "prompt": current_message, "response": observation.response,
                "metrics": {key: round_metrics.get(key, UNAVAILABLE) for key in RUNTIME_METRICS},
                "trace_id": observation.trace_id,
                "metrics_incomplete_reason": observation.metrics_incomplete_reason,
            }
            result["interactions"].append(interaction)
            result["final_response"] = observation.response
            if not checklist_enabled:
                # One-interaction external Replay needs no auxiliary Checklist call.
                break
            try:
                report = judge(
                    harness=harness, checklist=copy.deepcopy(checklist),
                    interactions=copy.deepcopy(result["interactions"]), messages=copy.deepcopy(result["messages"]),
                    artifacts=copy.deepcopy(result["artifacts"]),
                )
                report["rounds"] = turn
            except Exception as exc:
                if checklist_required:
                    raise
                result.setdefault("auxiliary_errors", []).append({
                    "stage": "judge", "type": type(exc).__name__,
                    "error_code": "REPLAY_AUXILIARY_JUDGE_UNAVAILABLE",
                })
                report = {"judge": "unavailable", "all_satisfied": False, "rounds": turn}
            result["checklist_report"] = copy.deepcopy(report)
            interaction["checklist_report"] = copy.deepcopy(report)
            if report.get("judge") == "unavailable":
                if checklist_required:
                    result["error_code"] = "REPLAY_JUDGE_UNAVAILABLE"
                    raise RuntimeError("Checklist judge unavailable")
                break
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            if report.get("all_satisfied"):
                result.update(completed=True, status="completed")
                break
            if turn >= max_interactions:
                break
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            selected = select_disclosure_items(
                checklist=checklist, report=report, disclosed_ids=disclosed_ids, batch_size=batch_size,
            )
            if not selected:
                break
            try:
                current_message = feedback(
                    harness=harness, response=observation.response,
                    positive_observations=list(report.get("positive_observations") or []),
                    selected_items=copy.deepcopy(selected), disclosed_requirements=list(disclosed_texts),
                    round_number=turn + 1,
                )
            except Exception as exc:
                if checklist_required:
                    raise
                result.setdefault("auxiliary_errors", []).append({
                    "stage": "feedback", "type": type(exc).__name__,
                    "error_code": "REPLAY_AUXILIARY_FEEDBACK_UNAVAILABLE",
                })
                break
            result["disclosures"].append({
                "after_turn": turn, "ids": [item["id"] for item in selected], "message": current_message,
            })
            for item in selected:
                if item["id"] not in disclosed_ids:
                    disclosed_texts.append(item["text"])
                disclosed_ids.add(item["id"])
        result.update(
            ok=True, status="completed" if not checklist_enabled or result["completed"] else "checklist_incomplete",
        )
        if context.execution_plan is not None:
            result["execution_verification"] = "verified"
    except ReplayUnsupported as exc:
        result.update(status="unsupported", error_code="REPLAY_UNSUPPORTED", error=str(exc))
        if context.execution_plan is not None:
            result["execution_verification"] = "unsupported"
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        result.setdefault("error_code", "TIMEOUT" if isinstance(exc, TimeoutError) else "REPLAY_FAILED")
    finally:
        if session is not None and callable(getattr(session, "close", None)):
            try:
                session.close()
            except Exception as exc:
                result.update(ok=False, status="failed", error_code="REPLAY_CLEANUP_FAILED", error=str(exc))
        result.update(
            interaction_turns=len(result["interactions"]), elapsed_seconds=round(time.monotonic() - started, 6),
            **{
                key: totals[key] if key in totals and key not in unavailable else UNAVAILABLE
                for key in RUNTIME_METRICS
            },
        )
    return result
