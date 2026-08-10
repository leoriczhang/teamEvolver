from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import teamEvolver.evolve.runtime.orchestrator as orchestrator_module
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.evidence import SkillEvidenceStore, _entry_from_session
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.evolve.stages.execute import (
    _build_evaluation_cohort_context,
    _build_session_evidence as build_planner_evidence,
    _parse_evolve_result,
)
from teamEvolver.evolve.stages.judge import _extract_output_artifacts
from teamEvolver.evolve.stages.verify import (
    _build_session_evidence as build_verifier_evidence,
)


def test_candidate_without_team_skill_evidence_is_suppressed() -> None:
    result = _parse_evolve_result(
        json.dumps(
            {
                "action": "optimize_description",
                "rationale": "The user requested a presentation.",
                "evidence_classification": {
                    "team_skill": [],
                    "user_memory": [],
                    "task_requirement": ["Create an HTML presentation."],
                    "agent_runtime": ["Generation was interrupted."],
                    "insufficient_evidence": [],
                },
                "skill": {
                    "name": "design-taste-frontend",
                    "description": "NOT for HTML presentations.",
                },
            }
        ),
        "design-taste-frontend",
    )

    assert result is not None
    assert result["action"] == DecisionAction.SKIP
    assert result["evidence_classification"]["task_requirement"]
    assert "no reusable team-skill evidence" in result["rationale"]


def test_candidate_with_causal_team_skill_evidence_is_preserved() -> None:
    result = _parse_evolve_result(
        json.dumps(
            {
                "action": "improve_skill",
                "rationale": "Artifacts need a reusable verification step.",
                "evidence_classification": {
                    "team_skill": [
                        {
                            "claim": "Verify the generated artifact before completion.",
                            "supporting_session_ids": ["session-1", "session-2"],
                            "causal_link": "Both runs claimed completion without checking the file.",
                        }
                    ],
                    "user_memory": [],
                    "task_requirement": [],
                    "agent_runtime": [],
                    "insufficient_evidence": [],
                },
                "skill": {
                    "name": "design-taste-frontend",
                    "description": "Frontend visual design workflow.",
                    "content": "Verify the generated artifact before completion.",
                },
            }
        ),
        "design-taste-frontend",
    )

    assert result is not None
    assert result["action"] == DecisionAction.IMPROVE
    assert result["evidence_classification"]["team_skill"][0]["supporting_session_ids"] == [
        "session-1",
        "session-2",
    ]


def test_truncated_evolve_json_is_repaired_before_validation() -> None:
    payload = json.dumps(
        {
            "action": "improve_skill",
            "rationale": "Multiple users converged on one acceptance rule.",
            "evidence_classification": {
                "team_skill": [
                    {
                        "claim": "Apply the shared acceptance rule.",
                        "supporting_session_ids": ["session-1", "session-2"],
                        "causal_link": "Both artifact checks failed without it.",
                    }
                ],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": [],
            },
            "skill": {
                "name": "html-ppt-methodology",
                "description": "HTML presentation workflow.",
                "content": "Apply the shared acceptance rule.",
            },
        }
    )

    result = _parse_evolve_result(payload[:-1], "html-ppt-methodology")

    assert result is not None
    assert result["action"] == DecisionAction.IMPROVE
    assert result["skill"]["content"] == "Apply the shared acceptance rule."


def test_evaluation_profile_is_visible_to_planner_and_verifier(
    tmp_path: Path,
) -> None:
    session = {
        "session_id": "session-profile",
        "runtime_context": {
            "evaluation_profile": "html_ppt_methodology_v1",
        },
        "_summary": "Four users converged on the same artifact requirements.",
        "_trajectory": "Generated and reparsed the HTML artifact.",
    }

    planner_evidence = build_planner_evidence([session])
    verifier_evidence = build_verifier_evidence([session])
    cohort_context = _build_evaluation_cohort_context(
        [
            session,
            {
                **session,
                "session_id": "session-profile-2",
            },
        ]
    )
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
        )
    )
    persisted_evidence = server._build_validation_evidence([session])

    assert "evaluation_profile: html_ppt_methodology_v1" in planner_evidence
    assert verifier_evidence[0]["evaluation_profile"] == "html_ppt_methodology_v1"
    assert "html_ppt_methodology_v1" in cohort_context
    assert "session-profile-2" in cohort_context
    assert persisted_evidence[0]["evaluation_profile"] == (
        "html_ppt_methodology_v1"
    )


def test_persisted_evaluation_profile_survives_evidence_rehydration() -> None:
    entry = _entry_from_session(
        {
            "session_id": "session-profile",
            "runtime_context": {
                "evaluation_profile": "html_ppt_methodology_v1",
            },
        }
    )

    restored = SkillEvidenceStore._synthetic_session(entry, "historical")

    assert entry["evaluation_profile"] == "html_ppt_methodology_v1"
    assert restored["runtime_context"]["evaluation_profile"] == (
        "html_ppt_methodology_v1"
    )


@pytest.mark.anyio
async def test_open_candidate_is_used_as_next_evolution_draft(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
        )
    )
    server._validation_store.save_job(
        {
            "job_id": "job-html",
            "candidate_skill_name": "html-ppt-methodology",
            "candidate_revision": 3,
            "updated_at": "2026-08-05T00:00:00+00:00",
            "candidate_skill": {
                "name": "html-ppt-methodology",
                "content": "candidate draft with learned rules",
            },
        }
    )
    published = {
        "name": "html-ppt-methodology",
        "content": "published seed",
        "_version": 1,
    }

    working = await server._working_skill_for_evolution(
        "html-ppt-methodology",
        published,
    )

    assert working is not None
    assert working["content"] == "candidate draft with learned rules"
    assert working["_version"] == 1
    assert working["_candidate_job_id"] == "job-html"
    assert working["_candidate_revision"] == 3


def test_candidate_audit_is_excluded_from_true_replay_cases(
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
        )
    )
    cases = server._build_replay_cases(
        [
            {
                "session_id": "candidate-audit",
                "source": "managed_agent_candidate_audit",
                "runtime_context": {"candidate_job_id": "job-1"},
                "turns": [
                    {
                        "turn_num": 1,
                        "prompt_text": "Audit the candidate.",
                        "response_text": "Two candidate gaps remain.",
                    }
                ],
            },
            {
                "session_id": "original-user-task",
                "turns": [
                    {
                        "turn_num": 1,
                        "prompt_text": "Create the requested HTML deck.",
                        "response_text": "Created artifacts/deck.html.",
                    }
                ],
            },
        ]
    )

    assert len(cases) == 1
    assert cases[0]["session_id"] == "original-user-task"


@pytest.mark.anyio
async def test_true_replay_subprocess_receives_parent_job_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    server = EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="test-key",
        )
    )
    job = {
        "job_id": "job-1",
        "candidate_skill": {
            "name": "design-taste-frontend",
            "description": "Frontend visual design workflow.",
            "content": "Use evidence.",
        },
        "replay_cases": [
            {
                "session_id": "session-1",
                "instruction": "Create an HTML presentation.",
                "evidence_window": "recent",
            }
        ],
        "checklist": {
            "format": "common_checklist_v2",
            "commonality": {"passed": True},
            "items": [
                {
                    "id": "execution_complete",
                    "kind": "hard",
                    "required": True,
                }
            ],
        },
        "min_score": 0.75,
    }
    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        job_file = Path(cmd[cmd.index("--job-file") + 1])
        captured.update(json.loads(job_file.read_text("utf-8")))
        verdict = {
            "status": "evaluated",
            "accepted": True,
            "no_regression": True,
            "quality_ok": True,
            "score": 0.9,
            "baseline_mean": 0.7,
            "case_count": 1,
            "cases": [],
            "checklist_results": {
                "baseline": {
                    "passed": False,
                    "hard_pass": False,
                    "pass_rate": 0.0,
                    "items": [
                        {
                            "id": "execution_complete",
                            "kind": "hard",
                            "required": True,
                            "passed": False,
                        }
                    ],
                },
                "candidate": {
                    "passed": True,
                    "hard_pass": True,
                    "pass_rate": 1.0,
                    "items": [
                        {
                            "id": "execution_complete",
                            "kind": "hard",
                            "required": True,
                            "passed": True,
                        }
                    ],
                },
            },
            "efficiency": {
                "baseline": {
                    "interaction_turns": 2,
                    "tool_call_count": 2,
                    "total_tokens": 200,
                },
                "candidate": {
                    "interaction_turns": 1,
                    "tool_call_count": 2,
                    "total_tokens": 200,
                },
            },
        }
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=(
                "TRUE_REPLAY_JSON_BEGIN\n"
                + json.dumps(verdict)
                + "\nTRUE_REPLAY_JSON_END\n"
            ),
        )

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)
    result = await server._run_candidate_replay(job)

    assert captured["job_id"] == "job-1"
    assert captured["candidate_skill"]["name"] == "design-taste-frontend"
    assert result["status"] == "evaluated"
    assert result["accepted"] is True


def test_validation_job_restores_verifier_evidence() -> None:
    sessions = EvolveServer._restore_validation_sessions(
        {
            "session_evidence": [
                {
                    "session_id": "session-1",
                    "summary": "The artifact was not verified.",
                    "judge_overall_score": 0.6,
                    "evaluation_profile": "html_ppt_methodology_v1",
                }
            ],
            "replay_cases": [
                {
                    "session_id": "session-1",
                    "turn_num": 1,
                    "instruction": "Create an HTML presentation.",
                    "reference_response": "Created the presentation.",
                }
            ],
        }
    )

    assert sessions[0]["_summary"] == "The artifact was not verified."
    assert sessions[0]["_judge_scores"]["overall_score"] == 0.6
    assert sessions[0]["runtime_context"]["evaluation_profile"] == (
        "html_ppt_methodology_v1"
    )
    assert sessions[0]["turns"][0]["prompt_text"] == "Create an HTML presentation."


def test_general_skill_output_path_is_extracted_as_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "deck.html"
    artifact.write_text("<html><body>11 slide deck</body></html>", encoding="utf-8")

    artifacts = _extract_output_artifacts(
        {
            "turns": [
                {
                    "tool_results": [
                        {
                            "tool_name": "general_skill.design-taste-frontend",
                            "result": {
                                "success": True,
                                "data": {
                                    "output_path": str(artifact),
                                    "slide_count": 11,
                                },
                            },
                        }
                    ]
                }
            ]
        }
    )

    assert artifacts == [
        {
            "path": str(artifact),
            "content": "<html><body>11 slide deck</body></html>",
        }
    ]
