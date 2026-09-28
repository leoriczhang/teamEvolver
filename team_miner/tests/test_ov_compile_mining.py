from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MINER_ROOT = Path(__file__).resolve().parents[1]
if str(MINER_ROOT) not in sys.path:
    sys.path.insert(0, str(MINER_ROOT))
if str(MINER_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(MINER_ROOT.parent))

from team_miner.lifecycle import mined_artifact_descriptor  # noqa: E402
from team_miner.mining_jobs import MiningJobManager  # noqa: E402
from team_miner.ov_compile_mining import (  # noqa: E402
    OvSkillMiningRepository,
    validate_descendant,
    validate_resource_uri,
)


def _repository(tmp_path: Path) -> OvSkillMiningRepository:
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")


def test_source_registry_is_reference_only_and_idempotent(tmp_path):
    repository = _repository(tmp_path)
    uri = "viking://resources/team/runbooks"

    first = repository.save_source("account-a", uri)
    second = repository.save_source("account-a", uri)

    assert first == second
    assert repository.list_source_records() == [first]
    removed = repository.remove_source(first["source_id"])
    assert removed["uri"] == uri
    assert repository.list_source_records() == []


def test_resource_uri_boundaries_reject_escape():
    root = validate_resource_uri("viking://resources/team/runbooks")
    assert validate_descendant(root, f"{root}/guide.md") == f"{root}/guide.md"
    with pytest.raises(ValueError, match="Resources"):
        validate_resource_uri("viking://agent/skills/private")
    with pytest.raises(ValueError, match="不在"):
        validate_descendant(root, "viking://resources/other/guide.md")


def test_materialized_compile_bundle_keeps_existing_candidate_contract(tmp_path):
    repository = _repository(tmp_path)
    job = repository.create_job(
        name="Support Skill",
        account="account-a",
        source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=3,
        max_rounds=3,
    )
    repository.write_output(
        job["job_id"],
        "compiled_skill/support-triage/SKILL.md",
        "---\nname: support-triage\ndescription: Triage support requests.\n---\n\n# Support triage\n",
    )
    repository.write_output(
        job["job_id"],
        "compiled_skill/support-triage/EVALUATION.md",
        "# Evaluation\n\nVerify routing and boundaries.\n",
    )
    cases = [
        {
            "input": "A customer reports an unknown billing error. What should the agent do?",
            "requirements": [f"requirement {index}" for index in range(1, 13)],
            "difficulty": "medium",
            "target_dimensions": ["routing", "safety"],
            "source_session_ids": ["viking://resources/team/runbooks/billing.md"],
        }
    ]
    repository.write_output(
        job["job_id"],
        "compiled_skill/support-triage/benchmark_cases.json",
        json.dumps(cases),
    )

    finalized = repository.finalize_materialized(job["job_id"])
    public = repository.public_job(finalized, detail=True)

    assert finalized["materialized"] is True
    assert finalized["artifact_quality"]["can_submit"] is True
    assert public["skills"] == [
        {
            "name": "support-triage",
            "has_skill": True,
            "has_evaluation": True,
            "has_benchmark": True,
            "question_count": 1,
        }
    ]
    workspace = repository.workspace(finalized)
    assert (workspace / "compiled_skill/support-triage/benchmark.json").is_file()
    assert (workspace / "compiled_skill/support-triage/BENCHMARK.md").is_file()
    descriptor = mined_artifact_descriptor("support-triage", skillminer_root=workspace)
    assert descriptor["question_count"] == 1
    assert descriptor["has_evaluation"] is True


def test_legacy_job_manager_does_not_take_over_ov_compile_jobs(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path)
    job = repository.create_job(
        name="OV task",
        account="account-a",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=1,
        max_rounds=1,
    )
    repository.update_job(job["job_id"], status="queued", ov_task_id="task-1")

    manager = MiningJobManager(tmp_path, start_immediately=False)
    manager.start()

    assert manager.get_job(job["job_id"])["status"] == "queued"
    assert manager._processes == {}
