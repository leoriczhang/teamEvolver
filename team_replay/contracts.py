"""Public Replay request and result contracts.

The contract describes a comparison without exposing how Skill or Memory
artifacts are produced. Producers submit immutable branch treatments and a
normalized Test Dataset; Replay owns execution and evaluation only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from .datasets.schema import DATASET_SCHEMA_V2

REPLAY_SPEC_SCHEMA_V1 = "team-replay.spec.v1"
REPLAY_RUN_RESULT_SCHEMA_V1 = "team-replay.run-result.v1"
CHECKLIST_FIRST_POLICY_V1 = "checklist-first-efficiency-v1"

SubjectKind = Literal["skill", "skill_set", "memory"]


class ReplayContractError(ValueError):
    """Raised when a producer submits an invalid Replay specification."""


@dataclass(frozen=True)
class ReplayTreatment:
    kind: str
    revision: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    artifact_ref: str = ""
    content_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "revision": self.revision,
            "payload": deepcopy(dict(self.payload)),
            "artifact_ref": self.artifact_ref,
            "content_hash": self.content_hash,
        }


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
    policy_id: str = CHECKLIST_FIRST_POLICY_V1
    runtime: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ReplayContractError("run_id is required")
        if self.subject_kind not in {"skill", "skill_set", "memory"}:
            raise ReplayContractError("subject_kind must be skill, skill_set or memory")
        if self.subject_kind == "skill_set" and not self.subject_ids:
            raise ReplayContractError("skill_set requires subject_ids")
        if self.subject_kind != "skill_set" and not self.subject_id.strip():
            raise ReplayContractError("subject_id is required")
        if not self.cases:
            raise ReplayContractError("Test Dataset must contain at least one case")
        if self.policy_id != CHECKLIST_FIRST_POLICY_V1:
            raise ReplayContractError(
                f"unsupported Replay policy: {self.policy_id}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPLAY_SPEC_SCHEMA_V1,
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
    subject_id = str(
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
    dataset_format = next(
        (
            str(case.get("dataset_format") or "")
            for case in cases
            if str(case.get("dataset_format") or "").strip()
        ),
        DATASET_SCHEMA_V2,
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
    treatment_kind = (
        "skill_set"
        if isinstance(candidate.get("skills"), list)
        else "skill_bundle"
    )
    return ReplaySpec(
        run_id=run_id,
        account_id=str(account_id or "default"),
        subject_kind="skill_set" if len(subject_ids) > 1 else "skill",
        subject_id=subject_id,
        subject_ids=subject_ids,
        dataset_format=dataset_format,
        cases=cases,
        baseline=ReplayTreatment(
            kind=treatment_kind,
            revision=baseline_revision,
            payload=baseline,
        ),
        candidate=ReplayTreatment(
            kind=treatment_kind,
            revision=revision,
            payload=candidate,
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
            # Transitional compatibility data. The common fields above remain
            # authoritative while callers migrate off the legacy job shape.
            "legacy_job": deepcopy(dict(job)),
        },
    )


def skill_job_from_spec(spec: ReplaySpec) -> dict[str, Any]:
    """Project a Skill Replay specification onto the legacy engine payload."""

    if spec.subject_kind not in {"skill", "skill_set"}:
        raise ReplayContractError("Skill Replay engine requires a Skill subject")
    legacy = spec.metadata.get("legacy_job")
    job = deepcopy(dict(legacy)) if isinstance(legacy, Mapping) else {}
    job.update({
        "job_id": spec.run_id,
        "skill_name": spec.subject_id,
        "candidate_skill_name": spec.subject_id,
        "skill_ids": list(spec.subject_ids) or [spec.subject_id],
        "candidate_revision": spec.candidate.revision,
        "candidate_skill": deepcopy(dict(spec.candidate.payload)),
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
    })
    return job
