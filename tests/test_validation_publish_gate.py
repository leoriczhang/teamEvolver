from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.orchestrator import EvolveServer


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("third_result", "max_rejections", "expected_status"),
    [
        ({"decision": "reject", "accepted": False}, 1, "rejected"),
        ({"decision": "reject", "accepted": True}, 1, "rejected"),
        ({"decision": "accept", "rejected": True}, 1, "rejected"),
        ({"decision": "reject", "accepted": False}, 2, "published"),
        ({"decision": "inconclusive", "accepted": False}, 1, "published"),
    ],
)
async def test_publish_quorum_respects_rejection_threshold(
    tmp_path, monkeypatch, third_result, max_rejections, expected_status
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            llm_api_key="",
            validation_max_rejections=max_rejections,
            human_review_enabled=False,
        ),
        mock=True,
        mock_root=str(tmp_path),
    )
    job_id = "quorum-job"
    server._validation_store.save_job(
        {
            "job_id": job_id,
            "candidate_revision": 1,
            "candidate_skill_name": "quorum-skill",
            "candidate_skill": {
                "name": "quorum-skill",
                "description": "Quorum candidate",
                "content": "Perform the task.",
            },
            "proposed_action": DecisionAction.IMPROVE,
        }
    )
    for index, result in enumerate(
        [
            {"decision": "accept", "accepted": True},
            {"decision": "accept", "accepted": True},
            third_result,
        ]
    ):
        server._validation_store.save_result(
            job_id, f"validator-{index}", {"candidate_revision": 1, **result}
        )
    upload = AsyncMock(return_value=(DecisionAction.IMPROVE, True))
    monkeypatch.setattr(server, "_resolve_and_upload", upload)
    monkeypatch.setattr(server, "_mark_evidence_published", AsyncMock())

    records, summary = await server._finalize_validation_jobs()

    decision = server._validation_store.load_decision(job_id)
    assert decision["status"] == expected_status
    assert decision["accepted_count"] == 2
    assert decision["rejected_count"] == int(third_result["decision"] != "inconclusive")
    if expected_status == "rejected":
        upload.assert_not_awaited()
        assert summary["published"] == 0
        assert summary["rejected"] == 1
        assert records[0]["action"] == "validation_rejected"
    else:
        upload.assert_awaited_once()
        assert summary["published"] == 1
        assert summary["rejected"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_result",
    [
        {"candidate_revision": 1},
        {"candidate_bundle_tree_sha256": "outdated-tree"},
        {"validator_capabilities": []},
    ],
)
async def test_publish_ignores_rejections_for_other_candidate_contracts(
    tmp_path, monkeypatch, invalid_result
) -> None:
    server = EvolveServer(
        EvolveServerConfig(llm_api_key="", human_review_enabled=False),
        mock=True,
        mock_root=str(tmp_path),
    )
    job_id = "revised-job"
    contract = {
        "candidate_revision": 2,
        "candidate_bundle_tree_sha256": "current-tree",
        "validator_capabilities": ["skill.bundle.v1"],
    }
    server._validation_store.save_job(
        {
            "job_id": job_id,
            "candidate_revision": 2,
            "candidate_bundle_tree_sha256": "current-tree",
            "required_validator_capabilities": ["skill.bundle.v1"],
            "candidate_skill": {
                "name": "revised-skill",
                "description": "Revised candidate",
                "content": "Perform the task.",
            },
        }
    )
    for index in range(3):
        server._validation_store.save_result(
            job_id,
            f"validator-{index}",
            {**contract, "decision": "accept", "accepted": True},
        )
    server._validation_store.save_result(
        job_id,
        "invalid-validator",
        {**contract, **invalid_result, "decision": "reject", "accepted": False},
    )
    upload = AsyncMock(return_value=(DecisionAction.CREATE, True))
    monkeypatch.setattr(server, "_resolve_and_upload", upload)
    monkeypatch.setattr(server, "_mark_evidence_published", AsyncMock())

    _, summary = await server._finalize_validation_jobs()

    assert summary["published"] == 1
    assert server._validation_store.load_decision(job_id)["rejected_count"] == 0
    upload.assert_awaited_once()
