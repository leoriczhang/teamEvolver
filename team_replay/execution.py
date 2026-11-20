"""System-owned Replay loop, independent of runtime transport and registration."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Mapping

from ._util import stable_hash
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
from .policy import normalize_case_checklist, normalize_checklist_report, progressive_config, select_disclosure_items


def run_branch(
    factory: ReplayAdapterFactory, context: ReplayContext, case: Mapping[str, Any],
    *, harness: Mapping[str, Any], max_interactions: int,
    judge: Callable = judge_checklist, feedback: Callable = render_user_feedback,
    session: ReplaySession | None = None,
) -> dict[str, Any]:
    """Only open(context), send(message) and close() touch the customer runtime."""
    started = time.monotonic()
    query = str(case.get("query") or "")
    checklist = normalize_case_checklist(case)
    result: dict[str, Any] = {
        "branch": context.treatment.branch, "runtime": context.runtime_type,
        "request_id": context.request_id, "ok": False, "completed": False,
        "context_input_hash": stable_hash(context.context_snapshot),
        "execution_manifest_hash": stable_hash({
            "runtime_type": context.runtime_type, "materials": context.materials,
            "timeout_seconds": context.timeout_seconds, "query": query,
            "max_interactions": max_interactions,
        }),
        "messages": [], "interactions": [], "artifacts": [], "disclosures": [],
        "trace_ids": [], "metrics_incomplete_reasons": [],
        "checklist_report": {"judge": "unavailable", "all_satisfied": False},
    }
    totals: dict[str, int | float] = {}
    unavailable: set[str] = set()
    current_message = query
    disclosed_ids: set[str] = set()
    disclosed_texts: list[str] = []
    batch_size = progressive_config(case)["batch_size"]
    turn_limit = max(1, min(20, max_interactions))
    lightweight = case.get("validation_method") == "candidate_execution"
    if lightweight:
        current_message += "\n\n本次任务的已知要求：\n" + "\n".join(item["text"] for item in checklist)
        turn_limit = min(2, turn_limit)
    try:
        if not query.strip() or not checklist:
            raise ValueError("Replay requires an initial query and a nonempty Checklist")
        if session is None:
            session = factory.open(copy.deepcopy(context))
        if not callable(getattr(session, "send", None)) or not callable(getattr(session, "close", None)):
            raise TypeError("factory.open(context) must return a ReplaySession with send() and close()")
        for turn in range(1, turn_limit + 1):
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            observation = validate_observation(session.send(current_message))
            if observation.execution_snapshot:
                result["execution_snapshot"] = copy.deepcopy(dict(observation.execution_snapshot))
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            round_metrics = dict(observation.metrics or {})
            if "tool_call_count" not in round_metrics and any(
                "tool_calls" in message for message in observation.messages
            ):
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
            judge_started = time.monotonic()
            previous_report = copy.deepcopy(result["checklist_report"]) if turn > 1 else None
            report = judge(
                harness={**harness, "timeout_seconds": min(120, int(harness.get("timeout_seconds") or 120)),
                         "previous_checklist_report": previous_report},
                checklist=copy.deepcopy(checklist),
                interactions=copy.deepcopy([interaction]), messages=copy.deepcopy(list(observation.messages)),
                artifacts=copy.deepcopy(list(observation.artifacts)),
            )
            report = normalize_checklist_report({"checklist_report": report}, expected_checklist=checklist)
            # Independent assessment has its own budget; do not charge its wait
            # against the Agent's execution deadline.
            judge_elapsed = time.monotonic() - judge_started
            started += judge_elapsed
            result["assessment_seconds"] = result.get("assessment_seconds", 0) + judge_elapsed
            accounting = getattr(session, "exclude_assessment_time", None)
            if accounting:
                accounting(judge_elapsed)
            report["rounds"] = turn
            result["checklist_report"] = copy.deepcopy(report)
            interaction["checklist_report"] = copy.deepcopy(report)
            if report.get("judge") == "unavailable":
                result["error_code"] = "REPLAY_JUDGE_UNAVAILABLE"
                result.update(status="assessment_pending", stop_reason="assessment_pending",
                              execution_status="returned", assessment_status="pending")
                break
            if report.get("external_blocked"):
                result.update(status="external_blocked", stop_reason="external_blocked",
                              external_blocked=True, blocking_conditions=report.get("blocking_conditions") or [])
                break
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            if report.get("all_satisfied"):
                result.update(completed=True, status="completed", stop_reason="checklist_satisfied")
                break
            if turn >= turn_limit:
                result["stop_reason"] = "interaction_limit"
                break
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            selected = ([item for item in checklist
                         if item["id"] in {r["id"] for r in report.get("items", []) if not r.get("satisfied")}]
                        if lightweight else select_disclosure_items(
                            checklist=checklist, report=report, disclosed_ids=disclosed_ids, batch_size=batch_size,
                        ))
            if not selected:
                raise RuntimeError("Checklist has unmet items but no continuation requirements")
            current_message = feedback(
                harness=harness, response=observation.response,
                positive_observations=list(report.get("positive_observations") or []),
                selected_items=copy.deepcopy(selected), disclosed_requirements=list(disclosed_texts),
                round_number=turn + 1,
            )
            result["disclosures"].append({
                "after_turn": turn, "ids": [item["id"] for item in selected], "message": current_message,
            })
            for item in selected:
                if item["id"] not in disclosed_ids:
                    disclosed_texts.append(item["text"])
                disclosed_ids.add(item["id"])
        result.update(ok=True)
        if result.get("status") not in {"external_blocked", "assessment_pending"}:
            result["status"] = "completed" if result["completed"] else "checklist_incomplete"
    except ReplayUnsupported as exc:
        result.update(status="unsupported", error_code="REPLAY_UNSUPPORTED", error=str(exc))
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        result.setdefault("error_code", "TIMEOUT" if isinstance(exc, TimeoutError) else "REPLAY_FAILED")
        result.setdefault("stop_reason", "timeout" if isinstance(exc, TimeoutError) else "branch_failure")
    finally:
        if session is not None and callable(getattr(session, "close", None)):
            try:
                session.close()
            except Exception as exc:
                result.update(ok=False, status="failed", error_code="REPLAY_CLEANUP_FAILED", error=str(exc))
            diagnostics = getattr(session, "diagnostics", None)
            if diagnostics:
                try:
                    evidence = diagnostics()
                    if isinstance(evidence, Mapping) and evidence:
                        result["runtime_diagnostics"] = copy.deepcopy(dict(evidence))
                        if evidence.get("execution_snapshot"):
                            result.setdefault("execution_snapshot", copy.deepcopy(evidence["execution_snapshot"]))
                except Exception:
                    result["metrics_incomplete_reasons"].append("Final runtime diagnostics unavailable")
        result.update(
            interaction_turns=len(result["interactions"]), elapsed_seconds=round(time.monotonic() - started, 6),
            **{
                key: totals[key] if key in totals and key not in unavailable else UNAVAILABLE
                for key in RUNTIME_METRICS
            },
        )
    return result
