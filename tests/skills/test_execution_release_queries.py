"""Immutable release evidence projections; no database, runtime or model calls."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from team_skills.evolution.repository import EvolutionRepository, digest
from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.release_queries import _validation, evaluation_summary, release_details
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.bundle import bundle_tree_sha256, encode_bundle_payload
from team_skills.library.repository import SkillNotFound
from tests.support.execution import component_change, profile_raw

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def evidence(monkeypatch):
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    profile = validate_profile(raw)
    baseline = plan_for(profile, profile["components"])
    changed = apply_changes(baseline, [component_change()], allowed_modules=["prompt"])
    release = {"profile_id": "instructor", "release_id": "candidate:original:r1", "candidate_id": "original",
               "candidate_revision": 1, "validation_ref": "validate:first", "restored_from": "",
               "document": {"execution_plan": changed, "origin": "candidate-review", "skill_dependencies": []}}
    payload = {"candidate_id": "original", "revision": 1, "detail": {
        "execution_profile_id": "instructor", "candidate_execution_plan": changed,
        "baseline_execution_plan": baseline, "base_release_id": "baseline", "skill_dependencies": [],
    }}
    evaluation = {
        "status": "evaluated", "candidate_revision": 1, "validation_run_id": "validate:first",
        "validation_verdict": "accept", "recommended_publish": False,
        "publication_gate": {"passed": False, "reasons": ["independent_validation_missing"]},
        "cost_gate": {"status": "passed", "total_tokens": None, "known_total_tokens": 123,
                      "accounting_status": "incomplete", "warnings": ["total_model_cost_unknown"],
                      "te_llm_usage": {"complete": False, "known_total_tokens": 12,
                                       "unknown_attempts": 3, "attempts": [{"private": "omit-me"}]}},
        "replay": {
            "verdict": "inconclusive", "case_count": 2,
            "efficiency": {"dimensions": {"total_tokens": {"baseline": 100, "candidate": 90, "delta": 10}}},
            "checklist": {"candidate": {"total": 4, "satisfied_count": 4, "reports": [{"private": "omit-me"}]}},
            "probe_gate": {"status": "passed", "runs": [
                {"kind": "aa", "job_id": "validate:first:aa", "verdict": "accept", "result": {"raw": "omit-me"}},
            ]},
            "cases": [{"query": "omit-me", "candidate": {"final_response": "omit-me"}}],
            "datasets": [{"inputs": [{"raw": "omit-me"}]}],
        },
    }
    command = {"command_id": "validate:first", "kind": "skill.validate", "payload": payload,
               "payload_hash": digest(payload), "result": {"status": "evaluated", "evaluation": evaluation},
               "finished_at": "2026-11-01T00:00:00Z"}
    commands = {command["command_id"]: command}

    async def read_command(self, conn, command_id):
        return copy.deepcopy(commands.get(command_id))

    monkeypatch.setattr(EvolutionRepository, "command", read_command)
    return SimpleNamespace(tx=SimpleNamespace(pool=SimpleNamespace(schema="test")), release=release,
                           command=command, commands=commands, evaluation=evaluation)


async def test_frozen_summary_preserves_original_metrics_unknowns_and_baseline_without_raw_bodies(evidence):
    before = copy.deepcopy(evidence.commands)
    value = await _validation(None, evidence.tx, evidence.release)
    assert value["status"] == "available"
    assert value["run_id"] == "validate:first"
    assert value["source_release_id"] is None
    assert value["evaluation_hash"] == digest(evidence.evaluation)
    assert value["baseline_execution_plan"] == evidence.command["payload"]["detail"]["baseline_execution_plan"]
    summary = value["evaluation"]
    assert summary["validation_verdict"] == "accept" and summary["replay"]["verdict"] == "inconclusive"
    assert summary["replay"]["efficiency"] == evidence.evaluation["replay"]["efficiency"]
    assert summary["cost_gate"]["total_tokens"] is None
    assert summary["cost_gate"]["te_llm_usage"]["unknown_attempts"] == 3
    assert "omit-me" not in str(summary)
    assert evidence.commands == before


async def test_reviewed_publication_reference_resolves_original_run_and_checks_its_hashes(evidence):
    payload = {"operation": "execution_publish_reviewed", "candidate_id": "original", "revision": 1,
               "validation_run_id": "validate:first", "validation_payload_hash": evidence.command["payload_hash"],
               "validation_result_hash": digest(evidence.evaluation)}
    publication = {"command_id": "publish:reviewed", "kind": "skill.evolve", "payload": payload,
                   "payload_hash": digest(payload), "finished_at": "2026-11-01T00:01:00Z",
                   "result": {"status": "published", "release_id": evidence.release["release_id"]}}
    evidence.commands[publication["command_id"]] = publication
    evidence.release["validation_ref"] = publication["command_id"]
    value = await _validation(None, evidence.tx, evidence.release)
    assert value["status"] == "available" and value["run_id"] == "validate:first"
    assert value["publication_command_id"] == "publish:reviewed"
    payload["validation_result_hash"] = "wrong"
    publication["payload_hash"] = digest(payload)
    assert (await _validation(None, evidence.tx, evidence.release))["reason"] == "publication_validation_hash_mismatch"


@pytest.mark.parametrize(("kind", "reason"), [
    ("missing", "validation_command_missing"),
    ("unfinished", "validation_not_completed"),
    ("missing_evaluation", "evaluation_missing"),
    ("payload_hash", "validation_command_mismatch"),
    ("candidate", "validation_command_mismatch"),
    ("revision", "validation_command_mismatch"),
    ("profile", "validation_profile_mismatch"),
    ("body", "validated_content_mismatch"),
    ("evaluation_revision", "evaluation_identity_mismatch"),
    ("baseline_profile", "baseline_profile_mismatch"),
])
async def test_missing_or_misbound_history_is_unavailable_not_current_candidate_fallback(evidence, kind, reason):
    command = evidence.command
    if kind == "missing":
        evidence.commands.clear()
    elif kind == "unfinished":
        command["finished_at"] = None
    elif kind == "missing_evaluation":
        command["result"].pop("evaluation")
    elif kind == "payload_hash":
        command["payload_hash"] = "wrong"
    elif kind in {"candidate", "revision"}:
        command["payload"]["candidate_id" if kind == "candidate" else "revision"] = "later"
    elif kind == "profile":
        command["payload"]["detail"]["execution_profile_id"] = "another-profile"
    elif kind == "body":
        evidence.release = copy.deepcopy(evidence.release)
        evidence.release["document"]["execution_plan"] = apply_changes(
            evidence.release["document"]["execution_plan"], [component_change("A different release")],
            allowed_modules=["prompt"],
        )
    elif kind == "evaluation_revision":
        command["result"]["evaluation"]["candidate_revision"] = 2
    elif kind == "baseline_profile":
        command["payload"]["detail"]["baseline_execution_plan"]["binding"]["profile_id"] = "another-profile"
    if kind != "payload_hash":
        command["payload_hash"] = digest(command["payload"])
    value = await _validation(None, evidence.tx, evidence.release)
    assert value["status"] == "unavailable" and value["reason"] == reason
    assert value["evaluation"] is None and value["baseline_execution_plan"] is None


@pytest.mark.parametrize("rollback", [False, True])
async def test_registration_and_rollback_do_not_claim_a_new_evaluation(evidence, rollback):
    release = copy.deepcopy(evidence.release)
    release.update(validation_ref="rollback:baseline" if rollback else "", restored_from="baseline" if rollback else "")
    release["document"]["origin"] = "baseline-registration"
    value = await _validation(None, evidence.tx, release)
    assert value["status"] == "not_applicable" and value["evaluation"] is None
    assert value["reason"] == ("rollback" if rollback else "baseline_registration")
    if rollback:
        assert value["source_release_id"] == "baseline"


async def test_profile_ownership_is_checked_before_returning_release_content(evidence, monkeypatch):
    async def profile(self, conn, profile_id):
        return {"current_release_id": "new-head"}

    async def release(self, conn, release_id):
        return evidence.release

    monkeypatch.setattr(ExecutionRepository, "profile", profile)
    monkeypatch.setattr(ExecutionRepository, "release", release)
    value = await release_details(None, evidence.tx, profile_id="instructor", release_id="old-release")
    assert value["release"] == evidence.release and value["current_release_id"] == "new-head"
    with pytest.raises(SkillNotFound):
        await release_details(None, evidence.tx, profile_id="other-profile", release_id="old-release")


def test_legacy_summary_does_not_invent_missing_metrics():
    assert evaluation_summary({"status": "evaluated", "replay": {"verdict": "reject"}}) == {
        "status": "evaluated", "replay": {"verdict": "reject"},
    }


def test_frozen_summary_keeps_external_scores_without_case_bodies_or_credentials():
    report = {"policy": "external-agent-score", "verdict": "accept", "accepted": True,
              "baseline_score": 70, "candidate_score": 85, "score_delta": 15,
              "min_score": 60, "min_improvement": 2, "case_count": 5, "evaluated_case_count": 5,
              "decision_basis": "external_score_improved"}
    evaluation = {"replay": {"verdict": "accept", "evaluation_policy_hash": "frozen-policy",
                            "decision_policy": copy.deepcopy(report), "external_evaluation": {
                                **report, "case_reports": [{"request": "omit-private-body"}],
                                "secret_ref": "omit-secret", "rubric": "omit-rubric",
                            }}}
    frozen = copy.deepcopy(evaluation)
    summary = evaluation_summary(evaluation)["replay"]
    assert summary["external_evaluation"] == report
    assert summary["decision_policy"]["score_delta"] == 15
    assert summary["evaluation_policy_hash"] == "frozen-policy"
    assert "omit-" not in str(summary)
    assert evaluation == frozen


@pytest.mark.parametrize(("reports", "case_count", "completed"), [
    ([{"all_satisfied": False}] * 6, 6, 0),
    ([{"all_satisfied": True}] * 6, 6, 6),
    (None, 6, None),
    ([{"all_satisfied": True}], 6, None),
    ([{}], 1, None),
])
def test_case_completion_counts_derive_only_from_complete_recorded_reports(reports, case_count, completed):
    side = {"case_count": case_count}
    if reports is not None:
        side["reports"] = reports
    summary = evaluation_summary({"replay": {"checklist": {"candidate": side}}})
    assert summary["replay"]["checklist"]["candidate"] == {
        "case_count": case_count, "completed_count": completed,
    }


@pytest.mark.parametrize("changed", [False, True])
async def test_joint_skill_dependencies_bind_frozen_bundle_and_all_peer_versions(evidence, changed):
    before = {"SKILL.md": b"before"}
    after = {"SKILL.md": b"after"} if changed else before
    payload = evidence.command["payload"]
    payload.update(skill_name="demo", base_version=3, tree_sha256=bundle_tree_sha256(after),
                   bundle=encode_bundle_payload(after), baseline=encode_bundle_payload(before))
    payload["detail"]["skill_dependencies"] = [
        {"name": "demo", "version": 3, "tree_sha256": bundle_tree_sha256(before)},
        {"name": "peer", "version": 2, "tree_sha256": "frozen-peer"},
    ]
    evidence.release["document"]["skill_dependencies"] = [
        {"name": "demo", "version": 4 if changed else 3, "tree_sha256": bundle_tree_sha256(after)},
        {"name": "peer", "version": 2, "tree_sha256": "frozen-peer"},
    ]
    evidence.command["payload_hash"] = digest(payload)
    assert (await _validation(None, evidence.tx, evidence.release))["status"] == "available"
    evidence.release["document"]["skill_dependencies"][1]["version"] = 99
    value = await _validation(None, evidence.tx, evidence.release)
    assert value["status"] == "unavailable" and value["reason"] == "validated_dependencies_mismatch"
