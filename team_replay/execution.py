"""System-owned Replay loop, independent of runtime transport and registration."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Mapping

from ._util import stable_hash
from .hooks import RUNTIME_METRICS, ReplayAdapterFactory, ReplayContext, ReplaySession, ReplayUnsupported, validate_observation
from .judging import judge_checklist, render_user_feedback
from .metrics import UNAVAILABLE, count_tool_calls
from .policy import normalize_case_checklist, progressive_config, select_disclosure_items


def run_branch(
    factory: ReplayAdapterFactory, context: ReplayContext, case: Mapping[str, Any],
    *, harness: Mapping[str, Any], max_interactions: int,
    judge: Callable = judge_checklist, feedback: Callable = render_user_feedback,
    session: ReplaySession | None = None,
) -> dict[str, Any]:
    """Only open(context), send(message) and close() touch the customer runtime."""
    started = time.monotonic()
    query = str(case.get("query") or case.get("instruction") or "")
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
    try:
        if not query.strip() or not checklist:
            raise ValueError("Replay requires an initial query and a nonempty Checklist")
        if session is None:
            session = factory.open(copy.deepcopy(context))
        if not callable(getattr(session, "send", None)) or not callable(getattr(session, "close", None)):
            raise TypeError("factory.open(context) must return a ReplaySession with send() and close()")
        for turn in range(1, max(1, min(20, max_interactions)) + 1):
            if time.monotonic() - started >= context.timeout_seconds:
                raise TimeoutError("Replay branch timeout")
            observation = validate_observation(session.send(current_message))
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
            report = judge(
                harness=harness, checklist=copy.deepcopy(checklist),
                interactions=copy.deepcopy(result["interactions"]), messages=copy.deepcopy(result["messages"]),
                artifacts=copy.deepcopy(result["artifacts"]),
            )
            report["rounds"] = turn
            result["checklist_report"] = copy.deepcopy(report)
            interaction["checklist_report"] = copy.deepcopy(report)
            if report.get("judge") == "unavailable":
                result["error_code"] = "REPLAY_JUDGE_UNAVAILABLE"
                raise RuntimeError("Checklist judge unavailable")
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
        result.update(ok=True, status="completed" if result["completed"] else "checklist_incomplete")
    except ReplayUnsupported as exc:
        result.update(status="unsupported", error_code="REPLAY_UNSUPPORTED", error=str(exc))
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
            **{key: totals[key] if key in totals and key not in unavailable else UNAVAILABLE for key in RUNTIME_METRICS},
        )
    return result
