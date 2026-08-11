from __future__ import annotations

from teamEvolver.evolve.runtime.checklist import (
    compile_common_checklist,
    scope_checklist_for_case,
)
from teamEvolver.storage import LocalObjectStore
from teamEvolver.validation.store import ValidationStore


def test_compile_common_checklist_persists_sources_and_profile_contract() -> None:
    checklist = compile_common_checklist(
        action="improve_skill",
        evidence_classification={
            "team_skill": [
                {
                    "claim": "Use exact parser assertions for the final HTML.",
                    "supporting_session_ids": ["s1", "s2"],
                    "causal_link": "Repeated artifact failures.",
                }
            ],
            "user_memory": ["User one prefers blue."],
        },
        session_evidence=[
            {
                "session_id": "s1",
                "user_alias": "u1",
                "evaluation_profile": "html_profile",
            },
            {
                "session_id": "s2",
                "user_alias": "u2",
                "evaluation_profile": "html_profile",
            },
        ],
        replay_cases=[],
    )

    assert checklist["format"] == "common_checklist_v2"
    assert checklist["commonality"]["passed"] is True
    assert checklist["source_user_aliases"] == ["u1", "u2"]
    assert checklist["excluded_personal_evidence"] == ["User one prefers blue."]
    assert any(item["id"] == "artifact_contract" for item in checklist["items"])
    assert any(item["id"] == "post_write_validation" for item in checklist["items"])


def test_single_session_claim_is_provisional_not_shared_checklist() -> None:
    checklist = compile_common_checklist(
        action="create_skill",
        evidence_classification={
            "team_skill": [
                {
                    "claim": "A one-off preference.",
                    "supporting_session_ids": ["s1"],
                }
            ]
        },
        session_evidence=[{"session_id": "s1", "user_alias": "u1"}],
        replay_cases=[],
    )

    assert checklist["commonality"]["passed"] is False
    assert checklist["commonality"]["provisional_claim_count"] == 1
    assert not any(
        item["id"].startswith("common_") for item in checklist["items"]
    )


def test_merge_checklist_is_union_of_current_and_published_target() -> None:
    checklist = compile_common_checklist(
        action="merge_skill",
        evidence_classification={
            "team_skill": [
                {
                    "claim": "Create the new summary artifact.",
                    "supporting_session_ids": ["new-1", "new-2"],
                }
            ]
        },
        session_evidence=[
            {"session_id": "new-1", "user_alias": "u1"},
            {"session_id": "new-2", "user_alias": "u2"},
        ],
        replay_cases=[],
        dedup={"most_similar_skill": "existing-skill", "similarity": 0.92},
        inherited_checklists=[
            {
                "skill_name": "existing-skill",
                "version": 3,
                "job_id": "published-job",
                "checklist": {
                    "source_session_ids": ["old-1", "old-2"],
                    "items": [
                        {
                            "id": "common_existing",
                            "claim": "Preserve the existing report workflow.",
                            "kind": "soft",
                            "evaluator": "llm_checklist",
                            "required": True,
                            "scope": "source_sessions",
                            "source_session_ids": ["old-1", "old-2"],
                        }
                    ],
                },
            }
        ],
    )

    assert checklist["format"] == "common_checklist_v2"
    assert any(item["id"] == "common_existing" for item in checklist["items"])
    assert checklist["merge_context"]["union_item_count"] == len(
        checklist["items"]
    )
    assert checklist["merge_context"]["checklist_sources"][1] == {
        "skill_name": "existing-skill",
        "version": 3,
        "inherited": True,
        "required_item_ids": ["common_existing"],
        "source_job_id": "published-job",
    }

    recent = scope_checklist_for_case(
        checklist,
        {"session_id": "new-1", "evidence_window": "recent"},
    )
    historical = scope_checklist_for_case(
        checklist,
        {"session_id": "old-1", "evidence_window": "historical"},
    )
    assert "common_existing" not in {
        item["id"] for item in recent["items"]
    }
    assert "common_existing" in {
        item["id"] for item in historical["items"]
    }


def test_published_decision_persists_versioned_checklist_context(
    tmp_path,
) -> None:
    store = ValidationStore.from_bucket(bucket=LocalObjectStore(str(tmp_path)))
    store.save_job(
        {
            "job_id": "job-1",
            "candidate_skill": {
                "name": "existing-skill",
                "description": "test",
                "content": "procedure",
            },
            "checklist": {
                "format": "common_checklist_v2",
                "items": [{"id": "existing", "required": True}],
            },
            "replay_cases": [{"session_id": "old-1", "instruction": "do work"}],
        }
    )
    store.save_evaluation("job-1", {"status": "evaluated"})
    store.save_decision(
        "job-1",
        {
            "status": "published",
            "skill_name": "existing-skill",
            "version": 4,
        },
    )

    context = store.load_skill_version_context("existing-skill", 4)

    assert context is not None
    assert context["job"]["checklist"]["format"] == "common_checklist_v2"
    assert context["job"]["replay_cases"][0]["session_id"] == "old-1"
