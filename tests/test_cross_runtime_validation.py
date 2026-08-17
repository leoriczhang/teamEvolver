from __future__ import annotations

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.integrations.agent_registry import register_agent
from teamEvolver.session_store import SessionStore
from teamEvolver.skills.frontmatter import enrich_manifest_entry
from teamEvolver.validation.runtime_compatibility import (
    evaluate_runtime_compatibility,
    prepare_runtime_validation,
    reconcile_open_validation_jobs,
    skill_supports_runtime,
)
from teamEvolver.validation.store import ValidationStore


def _agent(runtime: str, *, replay: bool = True) -> dict:
    return {
        "agent_id": f"{runtime}:test",
        "runtime_type": runtime,
        "status": "active",
        "capability_ids": (
            ["replay.branch.v1", "skill.sync.v1"]
            if replay
            else ["session.ingest.v1"]
        ),
        "metadata": (
            {"tenant_id": "tenant-1"}
            if runtime == "agentshub"
            else {"profile_id": "profile-1"}
        ),
    }


def _case(runtime: str = "hermes") -> dict:
    return {
        "case_id": "case-1",
        "dataset_id": "case-1",
        "dataset_format": "teamEvolver-progressive-test.v1",
        "session_id": "source-session",
        "instruction": "Complete the held-out task.",
        "checklist": [{"id": "check-1", "text": "Task completed"}],
        "source_runtime": {
            "type": runtime,
            "integration_id": f"{runtime}:source",
        },
        "source_runtime_context": {"profile_id": "source-profile"},
        "evidence_window": "recent",
    }


def test_portable_skill_builds_neutral_case_for_each_runtime_class() -> None:
    prepared = prepare_runtime_validation(
        skill={
            "name": "portable-skill",
            "extra_frontmatter": {"portable": True},
        },
        sessions=[{"runtime": {"type": "hermes"}}],
        replay_cases=[_case()],
        agents=[_agent("hermes"), _agent("agentshub")],
    )

    policy = prepared["policy"]
    assert policy["required_runtimes"] == ["agentshub", "hermes"]
    compatibility = [
        case
        for case in prepared["replay_cases"]
        if case.get("compatibility_runtime") == "agentshub"
    ]
    assert len(compatibility) == 1
    neutral = compatibility[0]
    assert neutral["context_policy"] == {
        "mode": "neutral",
        "team_only": True,
        "personal_context": False,
    }
    assert neutral["source_runtime_context"] == {"tenant_id": "tenant-1"}
    assert "external_subject" not in neutral["source_runtime_context"]


def test_runtime_gate_never_aggregates_one_runtime_against_another() -> None:
    policy = {
        "required_runtimes": ["hermes", "agentshub"],
        "missing_replay_runtimes": [],
    }
    partial = evaluate_runtime_compatibility(
        policy,
        [
            {
                "runtime_validation": {
                    "hermes": {"accepted": True, "decision": "accept"}
                }
            }
        ],
    )
    rejected = evaluate_runtime_compatibility(
        policy,
        [
            {
                "runtime_validation": {
                    "hermes": {"accepted": True, "decision": "accept"},
                    "agentshub": {"accepted": False, "decision": "reject"},
                }
            }
        ],
    )
    passed = evaluate_runtime_compatibility(
        policy,
        [
            {
                "runtime_validation": {
                    "hermes": {"accepted": True, "decision": "accept"},
                    "agentshub": {"accepted": True, "decision": "accept"},
                }
            }
        ],
    )

    assert partial["status"] == "pending"
    assert rejected["status"] == "rejected"
    assert passed["status"] == "passed"


def test_ingestion_only_runtime_blocks_publish_without_replay_capability() -> None:
    prepared = prepare_runtime_validation(
        skill={
            "name": "ingest-skill",
            "extra_frontmatter": {
                "supported_runtimes": ["langfuse"],
            },
        },
        sessions=[{"runtime": {"type": "langfuse"}}],
        replay_cases=[_case("langfuse")],
        agents=[_agent("langfuse", replay=False)],
    )
    gate = evaluate_runtime_compatibility(
        prepared["policy"],
        [{"runtime_type": "langfuse", "accepted": True}],
    )

    assert prepared["policy"]["missing_replay_runtimes"] == ["langfuse"]
    assert gate["status"] == "blocked"


def test_runtime_specific_skill_is_only_distributed_to_supported_runtime() -> None:
    skill = {
        "name": "agentshub-only",
        "runtime_policy": {
            "portable": False,
            "supported_runtimes": ["agentshub"],
            "distribution_runtimes": ["agentshub"],
        },
    }

    assert skill_supports_runtime(skill, "agentshub") is True
    assert skill_supports_runtime(skill, "hermes") is False


def test_skill_frontmatter_persists_runtime_distribution_policy(
    tmp_path,
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: agentshub-only\n"
        "description: Runtime-specific skill\n"
        "portable: false\n"
        "supported_runtimes: [agentshub]\n"
        "---\n\nRun.\n",
        encoding="utf-8",
    )
    entry: dict = {}

    enrich_manifest_entry(entry, str(skill_path))

    assert entry["runtime_policy"] == {
        "portable": False,
        "supported_runtimes": ["agentshub"],
    }


def test_runtime_capability_matrix_blocks_missing_tool_surface() -> None:
    agent = _agent("agentshub")
    agent["capability_details"] = {
        "replay.branch.v1": {"tools": ["file", "bash"]},
        "skill.bundle.v1": {"formats": ["bundle-v1"]},
    }
    agent["capability_ids"].append("skill.bundle.v1")
    prepared = prepare_runtime_validation(
        skill={
            "name": "browser-skill",
            "extra_frontmatter": {
                "supported_runtimes": ["agentshub"],
                "required_tools": ["browser"],
                "bundle_format": "bundle-v1",
            },
        },
        sessions=[{"runtime": {"type": "agentshub"}}],
        replay_cases=[_case("agentshub")],
        agents=[agent],
    )
    gate = evaluate_runtime_compatibility(
        prepared["policy"],
        [
            {
                "runtime_type": "agentshub",
                "accepted": True,
                "decision": "accept",
            }
        ],
    )

    assert prepared["policy"]["incompatible_runtimes"] == {
        "agentshub": "missing tools: browser"
    }
    assert gate["status"] == "blocked"


@pytest.mark.anyio
async def test_publish_quorum_cannot_hide_missing_runtime_result(
    tmp_path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="",
            publish_mode="validated",
            validation_required_results=1,
            validation_required_approvals=1,
            human_review_enabled=False,
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    job_id = "portable-job"
    server._validation_store.save_job(
        {
            "job_id": job_id,
            "candidate_revision": 1,
            "candidate_skill_name": "portable-skill",
            "candidate_skill": {
                "name": "portable-skill",
                "description": "Portable candidate",
                "content": "Perform the task.",
            },
            "proposed_action": DecisionAction.CREATE,
            "runtime_validation_policy": {
                "required_runtimes": ["hermes", "agentshub"],
                "missing_replay_runtimes": [],
            },
            "session_ids": [],
        }
    )
    server._validation_store.save_result(
        job_id,
        "validator",
        {
            "candidate_revision": 1,
            "accepted": True,
            "decision": "accept",
            "runtime_validation": {
                "hermes": {"accepted": True, "decision": "accept"}
            },
        },
    )

    records, summary = await server._finalize_validation_jobs()

    assert records == []
    assert summary["published"] == 0
    assert summary["pending"] == 1
    assert server._validation_store.load_decision(job_id) is None


def test_legacy_job_reconciliation_migrates_exact_identity_and_supersedes_bad(
    tmp_path,
) -> None:
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_session_backend="viking",
        sharing_viking_endpoint="memory://" + str(tmp_path / "store"),
        users_registry_path=str(tmp_path / "users.json"),
    )
    register_agent(
        config,
        {
            "schema_version": "teamevolver.agent-registration.v1",
            "protocol_version": "1.0",
            "agent_id": "agentshub:tenant-1",
            "runtime_type": "agentshub",
            "capabilities": {"replay.branch.v1": {}},
            "metadata": {"tenant_id": "tenant-1"},
        },
    )
    sessions = SessionStore.from_config(config)
    sessions.save_queued(
        {
            "session_id": "valid-session",
            "source": "agentshub",
            "runtime": {
                "type": "agentshub",
                "integration_id": "agentshub:tenant-1",
            },
            "runtime_context": {"tenant_id": "tenant-1"},
            "turns": [{"prompt_text": "run", "response_text": "done"}],
        }
    )
    sessions.save_queued(
        {
            "session_id": "legacy-session",
            "source": "agentshub",
            "turns": [{"prompt_text": "run", "response_text": "done"}],
        }
    )
    store = ValidationStore.from_config(config)
    for job_id, session_id in (
        ("valid-job", "valid-session"),
        ("legacy-job", "legacy-session"),
    ):
        store.save_job(
            {
                "job_id": job_id,
                "candidate_revision": 1,
                "candidate_skill": {
                    "name": f"{job_id}-skill",
                    "description": "Candidate",
                    "content": "Run.",
                },
                "replay_cases": [
                    {
                        "session_id": session_id,
                        "instruction": "run",
                    }
                ],
            }
        )

    preview = reconcile_open_validation_jobs(config, dry_run=True)
    applied = reconcile_open_validation_jobs(config, dry_run=False)

    assert preview["migrated"] == 1
    assert preview["superseded"] == 1
    assert applied["migrated"] == 1
    assert applied["superseded"] == 1
    migrated = store.load_job("valid-job")
    assert migrated is not None
    assert migrated["candidate_revision"] == 2
    assert migrated["runtime_validation_policy"]["required_runtimes"] == [
        "agentshub"
    ]
    assert store.load_decision("legacy-job")["status"] == "superseded"
