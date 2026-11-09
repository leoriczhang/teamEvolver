"""Public Replay request and result contracts.

The contract describes a comparison without exposing how Skill or Memory
artifacts are produced. Producers submit immutable branch treatments and a
normalized Test Dataset; Replay owns execution and evaluation only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .datasets.schema import DATASET_SCHEMA
from .evaluation import EXTERNAL_AGENT_SCORE_POLICY, external_evaluation_enabled, normalize_evaluation
from .execution_plan import plan_from_job, treatment_execution_plan, validate_execution_plan_pair

REPLAY_SPEC_SCHEMA = "team-replay.spec"
REPLAY_RUN_RESULT_SCHEMA = "team-replay.run-result"
CHECKLIST_FIRST_POLICY = "checklist-first-efficiency"

SubjectKind = Literal["skill", "skill_set", "memory", "execution"]


class ReplayContractError(ValueError):
    """Raised when a producer submits an invalid Replay specification."""


@dataclass(frozen=True)
class ReplayTreatment:
    kind: str
    revision: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    artifact_ref: str = ""
    content_hash: str = ""
    execution_plan: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "kind": self.kind,
            "revision": self.revision,
            "payload": deepcopy(dict(self.payload)),
            "artifact_ref": self.artifact_ref,
            "content_hash": self.content_hash,
        }
        if self.execution_plan is not None:
            result["execution_plan"] = deepcopy(dict(self.execution_plan))
        return result


@dataclass(frozen=True)
class ReplaySpec:
    run_id: str
    subject_kind: SubjectKind
    subject_id: str
    dataset_format: str
    cases: tuple[Mapping[str, Any], ...]
    baseline: ReplayTreatment
    candidate: ReplayTreatment
    subject_ids: tuple[str, ...] = ()
    account_id: str = "default"
    policy_id: str = CHECKLIST_FIRST_POLICY
    runtime: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evaluation: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ReplayContractError("run_id is required")
        if self.subject_kind not in {"skill", "skill_set", "memory", "execution"}:
            raise ReplayContractError("subject_kind must be skill, skill_set, memory or execution")
        if self.subject_kind == "skill_set" and not self.subject_ids:
            raise ReplayContractError("skill_set requires subject_ids")
        if self.subject_kind != "skill_set" and not self.subject_id.strip():
            raise ReplayContractError("subject_id is required")
        if not self.cases:
            raise ReplayContractError("Test Dataset must contain at least one case")
        if self.dataset_format != DATASET_SCHEMA:
            raise ReplayContractError(f"dataset format must be {DATASET_SCHEMA}")
        external = external_evaluation_enabled(self.evaluation)
        expected_policy = EXTERNAL_AGENT_SCORE_POLICY if external else CHECKLIST_FIRST_POLICY
        if self.policy_id != expected_policy:
            raise ReplayContractError(
                f"unsupported Replay policy: {self.policy_id}"
            )
        validate_execution_plan_pair(
            treatment_execution_plan(self.baseline), treatment_execution_plan(self.candidate),
        )
        if self.subject_kind == "execution" and (
            self.baseline.payload or self.candidate.payload or self.subject_ids
            or treatment_execution_plan(self.baseline) is None
        ):
            raise ReplayContractError("execution Replay requires plans without a Skill treatment")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPLAY_SPEC_SCHEMA,
            "run_id": self.run_id,
            "account_id": self.account_id,
            "subject": {
                "kind": self.subject_kind,
                **({"id": self.subject_id} if self.subject_id else {}),
                **({"ids": list(self.subject_ids)} if self.subject_ids else {}),
            },
            "dataset": {
                "format": self.dataset_format,
                "cases": [deepcopy(dict(case)) for case in self.cases],
            },
            "branches": {
                "baseline": self.baseline.as_dict(),
                "candidate": self.candidate.as_dict(),
            },
            "runtime": deepcopy(dict(self.runtime)),
            "policy": {"id": self.policy_id},
            "limits": deepcopy(dict(self.limits)),
            "metadata": deepcopy(dict(self.metadata)),
            **({"evaluation": normalize_evaluation(self.evaluation)}
               if external_evaluation_enabled(self.evaluation) else {}),
        }


def skill_replay_spec(
    job: Mapping[str, Any],
    *,
    account_id: str = "default",
) -> ReplaySpec:
    """Adapt an existing Skill Candidate job to the common Replay contract."""

    run_id = str(job.get("job_id") or job.get("id") or "").strip()
    candidate = (
        dict(job.get("candidate_skill"))
        if isinstance(job.get("candidate_skill"), Mapping)
        else {}
    )
    baseline = (
        dict(job.get("current_skill"))
        if isinstance(job.get("current_skill"), Mapping)
        else {}
    )
    execution_only = job.get("candidate_kind") == "execution"
    subject_id = str((job.get("execution_profile_id") if execution_only else None) or
        candidate.get("name")
        or job.get("candidate_skill_name")
        or job.get("skill_name")
        or ""
    ).strip()
    subject_ids = tuple(sorted({
        str(item or "").strip()
        for item in job.get("skill_ids") or []
        if str(item or "").strip()
    }))
    cases = tuple(
        dict(case)
        for case in job.get("replay_cases") or []
        if isinstance(case, Mapping)
    )
    revision = str(
        job.get("candidate_revision")
        or candidate.get("tree_sha256")
        or "1"
    )
    baseline_revision = str(
        (job.get("baseline_ref") or {}).get("revision")
        if isinstance(job.get("baseline_ref"), Mapping)
        else ""
    )
    treatment_kind = "execution_plan" if execution_only else (
        "skill_set"
        if isinstance(candidate.get("skills"), list)
        else "skill_bundle"
    )
    plans = validate_execution_plan_pair(plan_from_job(job, "baseline"), plan_from_job(job, "candidate"))
    evaluation = job.get("evaluation") or {}
    return ReplaySpec(
        run_id=run_id,
        account_id=str(account_id or "default"),
        subject_kind="execution" if execution_only else ("skill_set" if len(subject_ids) > 1 else "skill"),
        subject_id=subject_id,
        subject_ids=subject_ids,
        evaluation=deepcopy(evaluation),
        policy_id=EXTERNAL_AGENT_SCORE_POLICY if external_evaluation_enabled(evaluation) else CHECKLIST_FIRST_POLICY,
        dataset_format=DATASET_SCHEMA,
        cases=cases,
        baseline=ReplayTreatment(
            kind=treatment_kind,
            revision=baseline_revision,
            payload=baseline,
            execution_plan=plans[0],
        ),
        candidate=ReplayTreatment(
            kind=treatment_kind,
            revision=revision,
            payload=candidate,
            execution_plan=plans[1],
        ),
        runtime=(
            dict(job.get("runtime_validation_policy"))
            if isinstance(job.get("runtime_validation_policy"), Mapping)
            else {}
        ),
        limits={
            "max_interactions": max(1, int(job.get("max_interactions") or 4)),
        },
        metadata={
            "source": deepcopy(job.get("source") or {}),
        },
    )


def skill_job_from_spec(spec: ReplaySpec) -> dict[str, Any]:
    """Project a Skill Replay specification onto the engine input."""

    if spec.subject_kind not in {"skill", "skill_set", "execution"}:
        raise ReplayContractError("Skill Replay engine requires a Skill subject")
    plans = validate_execution_plan_pair(
        treatment_execution_plan(spec.baseline), treatment_execution_plan(spec.candidate),
    )
    return {
        "job_id": spec.run_id,
        "candidate_kind": "execution" if spec.subject_kind == "execution" else "skill",
        "execution_profile_id": spec.subject_id if spec.subject_kind == "execution" else "",
        "skill_name": None if spec.subject_kind == "execution" else spec.subject_id,
        "candidate_skill_name": None if spec.subject_kind == "execution" else spec.subject_id,
        "skill_ids": [] if spec.subject_kind == "execution" else (list(spec.subject_ids) or [spec.subject_id]),
        "candidate_revision": spec.candidate.revision,
        "candidate_skill": deepcopy(dict(spec.candidate.payload)) if spec.candidate.payload else None,
        "current_skill": (
            deepcopy(dict(spec.baseline.payload))
            if spec.baseline.payload
            else None
        ),
        "replay_cases": [deepcopy(dict(case)) for case in spec.cases],
        "runtime_validation_policy": deepcopy(dict(spec.runtime)),
        "max_interactions": max(
            1,
            int(spec.limits.get("max_interactions") or 4),
        ),
        "source": deepcopy(spec.metadata.get("source") or {}),
        **({"evaluation": normalize_evaluation(spec.evaluation)}
           if external_evaluation_enabled(spec.evaluation) else {}),
        **({
            "baseline_execution_plan": deepcopy(plans[0]),
            "candidate_execution_plan": deepcopy(plans[1]),
        } if plans[0] is not None or plans[1] is not None else {}),
    }
