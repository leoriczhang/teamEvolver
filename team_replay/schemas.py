"""Strict conversion of persistent Replay specs into the shared Contract."""

from __future__ import annotations

from .contracts import ReplaySpec, ReplayTreatment


def spec_from_dict(raw):
    branches = raw["branches"]
    subject = raw["subject"]
    return ReplaySpec(
        run_id=raw["run_id"], subject_kind=subject["kind"], subject_id=subject.get("id", ""),
        subject_ids=tuple(subject.get("ids", [])), account_id=raw.get("account_id", "default"),
        dataset_format=raw["dataset"]["format"], cases=tuple(raw["dataset"]["cases"]),
        baseline=ReplayTreatment(**branches["baseline"]), candidate=ReplayTreatment(**branches["candidate"]),
        runtime=raw.get("runtime", {}), limits=raw.get("limits", {}), metadata=raw.get("metadata", {}),
        policy_id=raw["policy"]["id"],
    )
