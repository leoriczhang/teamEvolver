# ruff: noqa: F401
"""Durable rounds, human Evidence supplements and immutable Compile snapshots."""

from __future__ import annotations

import json

import pytest

from team_miner.skill_miner.ov_compile_mining import MAX_PARALLEL_JOBS, REVIEW_SCHEMA_VERSION, OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_capability_plan, build_document


def create_job(repository, *, rounds=3, human=True):
    job = repository.create_job(
        name="Evidence rounds",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/round-1/source",
        output_uri="viking://resources/test/round-1/output",
        document_count=1,
        max_rounds=rounds,
        human_checkpoints=human,
    )
    return repository.update_job(
        job["job_id"], status="running", ov_task_id="round-1-task", started_at="2026-10-10T00:00:00Z"
    )

def question(source="viking://resources/team/source/policy.md"):
    return {
        "qid": "gap-1",
        "question": "适用阈值是否已确认？",
        "reason": "原文阈值存在冲突。",
        "source_refs": [source],
        "required": True,
    }

def populate(repository, job, *, questions=None, review=True):
    identity = job["job_id"]
    if int(job.get("candidate_contract_version") or 1) >= 2:
        repository.write_output(identity, "analysis/capability-plan.json", json.dumps(
            build_capability_plan(["policy-review"], f"{job['source_snapshot']}/policy.md")
        ))
    repository.write_output(identity, "analysis/evidence-map.md", "# Evidence\nSource policy.")
    repository.write_output(identity, "analysis/gaps.md", "# Gaps\nReview applicability.")
    if review:
        repository.write_output(
            identity,
            "analysis/review.json",
            json.dumps(
                {
                    "schema_version": REVIEW_SCHEMA_VERSION,
                    "questions": [question()] if questions is None else questions,
                }
            ),
        )
    repository.write_output(
        identity,
        "compiled_skill/policy-review/SKILL.md",
        "---\nname: policy-review\ndescription: Apply sourced rules.\n---\n# Policy",
    )
    repository.write_output(identity, "compiled_skill/policy-review/EVALUATION.md", "# Evaluation")
    repository.write_output(identity, "compiled_skill/policy-review/BENCHMARK.md", "# Dataset")
    document = build_document(
        "policy-review",
        [
            {
                "input": f"Scenario {i}",
                "requirements": [f"Condition {j}" for j in range(12)],
                "source_session_ids": [f"{job['source_snapshot']}/policy.md"],
            }
            for i in range(12)
        ],
    )
    repository.write_output(identity, "compiled_skill/policy-review/benchmark.json", json.dumps(document))

@pytest.fixture
def repository(tmp_path):
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner", backend=MemoryMetadataBackend())

def complete_round(repository, job, **kwargs):
    populate(repository, job, **kwargs)
    repository.finalize_materialized(job["job_id"])
    return repository.finish_materialized_round(job["job_id"])
