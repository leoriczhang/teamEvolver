"""Small external Interface for optional Replay execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import DEFAULT_REPLAY_CONFIG
from .contracts import (
    REPLAY_RUN_RESULT_SCHEMA,
    ReplaySpec,
    skill_job_from_spec,
)


def _subject(spec: ReplaySpec) -> dict[str, Any]:
    return {
        "kind": spec.subject_kind,
        **({"id": spec.subject_id} if spec.subject_id else {}),
        **({"ids": list(spec.subject_ids)} if spec.subject_ids else {}),
    }


@dataclass
class EmbeddedReplayGateway:
    """Adapter that runs the canonical engine in the current process."""

    evaluator: Callable[..., dict[str, Any]]

    def evaluate(
        self,
        spec: ReplaySpec,
        *,
        case_index: int | None = None,
        timeout_seconds: int = 600,
        max_interactions: int | None = None,
    ) -> dict[str, Any]:
        if spec.subject_kind not in {"skill", "skill_set"}:
            raise ValueError(
                "Embedded Skill gateway cannot materialize a Memory treatment"
            )
        interactions = max(
            1,
            int(
                max_interactions
                or spec.limits.get("max_interactions")
                or DEFAULT_REPLAY_CONFIG["max_interactions"]
            ),
        )
        result = self.evaluator(
            spec.run_id,
            job=skill_job_from_spec(spec),
            case_index=case_index,
            timeout=max(30, int(timeout_seconds or 600)),
            max_interactions=interactions,
        )
        return {
            "schema_version": REPLAY_RUN_RESULT_SCHEMA,
            "run_id": spec.run_id,
            "account_id": spec.account_id,
            "subject": _subject(spec),
            "policy": {"id": spec.policy_id},
            **result,
        }


class DisabledReplayGateway:
    """Adapter used when Replay is intentionally not enabled."""

    def __init__(self, reason: str = "Replay is disabled") -> None:
        self.reason = str(reason or "Replay is disabled")

    def evaluate(
        self,
        spec: ReplaySpec,
        *,
        case_index: int | None = None,
        timeout_seconds: int = 600,
        max_interactions: int | None = None,
    ) -> dict[str, Any]:
        del case_index, timeout_seconds, max_interactions
        return {
            "schema_version": REPLAY_RUN_RESULT_SCHEMA,
            "run_id": spec.run_id,
            "account_id": spec.account_id,
            "subject": _subject(spec),
            "policy": {"id": spec.policy_id},
            "status": "not_run",
            "accepted": False,
            "verdict": "inconclusive",
            "no_regression": False,
            "reason": self.reason,
            "cases": [],
        }


@dataclass
class HostReplayGateway:
    """Run the shared pure Engine through an explicitly injected ReplayHost."""
    host: object
    judge: Callable | None = None
    feedback: Callable | None = None

    def evaluate(self, spec, *, case_index=None, timeout_seconds=600, max_interactions=None):
        from dataclasses import replace

        from .runner import evaluate_spec

        pinned = replace(spec, limits={
            **spec.limits, "timeout_seconds": timeout_seconds,
            "max_interactions": max_interactions or spec.limits.get(
                "max_interactions", DEFAULT_REPLAY_CONFIG["max_interactions"],
            ),
        })
        return evaluate_spec(pinned, host=self.host, case_index=case_index, judge=self.judge, feedback=self.feedback)

    def rejudge(self, spec, *, case_index, result):
        import copy

        from .judging import judge_checklist
        from .policy import normalize_checklist_report
        from .runner import adjudicate_results

        case = spec.cases[case_index]
        pairs = copy.deepcopy(result.get("cases") or [])
        if len(pairs) != 1:
            raise ValueError("Archived Case must contain one branch pair")
        branches = {b: pairs[0].get(b) or {} for b in ("baseline", "candidate")}
        for branch in branches.values():
            if branch.get("status") != "assessment_pending":
                continue
            report = (self.judge or judge_checklist)(
                harness={**self.host.judge_harness(), "timeout_seconds": 120},
                checklist=copy.deepcopy(case["checks"]), interactions=branch.get("interactions") or [],
                messages=branch.get("messages") or [], artifacts=branch.get("artifacts") or [],
            )
            report = normalize_checklist_report({"checklist_report": report}, expected_checklist=case["checks"])
            branch["checklist_report"] = report
            if report.get("judge") != "unavailable":
                branch.update(completed=report["all_satisfied"],
                              status="completed" if report["all_satisfied"] else "checklist_incomplete",
                              assessment_status="completed", stop_reason="assessment_completed", ok=True)
                branch.pop("error_code", None)
        return {**adjudicate_results(spec, case, branches, adapter_revision=result.get("adapter_revision", ""),
                                    runtime_type=result.get("runtime_type", "")),
                "reused_from": result.get("reused_from"), "assessment_only": True}
