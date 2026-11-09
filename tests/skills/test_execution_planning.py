"""Pending Prompt edits and safe metrics reuse the shared evolution checkpoint."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from team_replay.evaluation import (
    evaluation_policy_hash,
    external_case_request,
    normalize_evaluation,
    normalize_external_report,
)
from team_skills.candidates.repository import CandidateStateError
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.planner import prompt_editor_input
from team_skills.execution.planning import (
    assert_planning_current,
    efficiency_feedback,
    freeze_prompt_planning,
    pending_context,
    restore_prompt_history,
    synthesis_context,
    validation_feedback,
)
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change, profile_raw
from tests.support.execution_feedback import analyzed_source, development_validation


def context():
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    profile = validate_profile(raw)
    return {"candidate_kind": "execution", "execution_profile_id": profile["profile_id"],
            "base_release_id": "published", "baseline_execution_plan": plan_for(profile, profile["components"]),
            "affected_skill_ids": [], "skill_dependencies": []}


def candidate():
    first = component_change("Pending A")
    first["evidence_refs"] = ["old-target"]
    detail = attach_execution_proposal({"component_changes": [first]}, {}, context=context(),
                                       allowed_modules=["prompt"], working_bundle={}, skill_changed=False)
    return {"candidate_id": "same-candidate", "revision": 3, "status": "open",
            "detail": detail["_execution_detail"], "evaluation": {
                "candidate_revision": 3, "validation_run_id": "do-not-send-id", "replay": {
                    "efficiency": {"baseline": {"interaction_turns": 1, "tool_call_count": 0, "total_tokens": 9},
                                   "candidate": {"interaction_turns": 2, "tool_call_count": 1, "total_tokens": 12}},
                    "cases": [{"query": "LOCKED-CANARY", "response": "LOCKED-CANARY",
                               "judge": "LOCKED-CANARY", "case_id": "LOCKED-ID"}],
                }}}


def test_pending_content_and_metrics_are_separate_from_unchanged_published_baseline():
    original, live = context(), candidate()
    before = copy.deepcopy((original, live))
    value = pending_context(original, live)
    assert value["baseline_execution_plan"] == original["baseline_execution_plan"]
    assert value["working_execution_plan"] == live["detail"]["candidate_execution_plan"]
    assert value["inherited_component_changes"][0]["evidence_refs"] == ["old-target"]
    assert value["active_candidate"]["revision"] == 3
    assert value["active_candidate_feedback"] == live["evaluation"]["replay"]["efficiency"]
    assert "LOCKED" not in json.dumps(value)
    assert "do-not-send-id" not in json.dumps(value)
    assert (original, live) == before


@pytest.mark.parametrize("invalid", [True, None, "9", -1, float("inf"), float("nan")])
def test_feedback_missing_or_invalid_numbers_are_unavailable_not_zero(invalid):
    evaluation = {"replay": {"efficiency": {"baseline": {"total_tokens": invalid, "query": "LOCKED"}}}}
    summary = efficiency_feedback(evaluation)
    assert summary["baseline"]["total_tokens"] == "unavailable"
    assert set(summary["candidate"].values()) == {"unavailable"}
    assert "LOCKED" not in json.dumps(summary)
    assert efficiency_feedback({}) == {}


def test_prompt_and_synthesis_receive_only_the_three_basic_efficiency_metrics():
    live = candidate()
    live["evaluation"]["replay"]["efficiency"]["baseline"].update(input_tokens=6, output_tokens=3)
    live["evaluation"]["replay"]["efficiency"]["candidate"].update(
        input_tokens=10, output_tokens=2, query="LOCKED", checklist="LOCKED", api_calls=99,
    )
    before = copy.deepcopy(live)
    frozen = pending_context(context(), live)
    feedback = frozen["active_candidate_feedback"]
    assert set(feedback["candidate"]) == {"interaction_turns", "tool_call_count", "total_tokens"}
    assert feedback["baseline"]["total_tokens"] == 9
    assert feedback["candidate"]["total_tokens"] == 12
    assert synthesis_context(frozen)["active_candidate_feedback"] == feedback
    assert "input_tokens" not in json.dumps(feedback) and "output_tokens" not in json.dumps(feedback)
    assert "LOCKED" not in json.dumps(feedback) and live == before


@pytest.mark.parametrize("field", ["base_release_id", "execution_profile_id"])
def test_incompatible_pending_subject_is_rejected(field):
    live = candidate()
    live["detail"][field] = "other"
    with pytest.raises(CandidateStateError, match="frozen execution baseline"):
        pending_context(context(), live)


def test_environment_change_and_missing_inherited_evidence_fail_closed():
    live = candidate()
    live["detail"]["baseline_execution_plan"]["environment"]["request"]["temperature"] = 0.8
    live["detail"]["candidate_execution_plan"]["environment"]["request"]["temperature"] = 0.8
    with pytest.raises(CandidateStateError):
        pending_context(context(), live)
    live = candidate()
    live["detail"]["component_changes"] = []
    with pytest.raises(CandidateStateError, match="evidence references are incomplete"):
        pending_context(context(), live)


def test_same_candidate_new_delta_retains_component_A_and_old_references():
    frozen = pending_context(context(), candidate())
    delta = component_change("New B", component_id="shared-context")
    delta["evidence_refs"] = ["new-target"]
    material = attach_execution_proposal({"component_changes": [delta]}, {}, context=frozen,
                                         allowed_modules=["prompt"], working_bundle={}, skill_changed=False)
    detail = material["_execution_detail"]
    parts = {p["component_id"]: p["content"] for p in detail["candidate_execution_plan"]["components"]}
    assert parts == {"main-prompt": "Pending A", "shared-context": "New B"}
    assert [c["evidence_refs"] for c in detail["component_changes"]] == [["old-target"], ["new-target"]]
    assert detail["baseline_execution_plan"]["components"] == context()["baseline_execution_plan"]["components"]
    again = component_change("Revised A")
    again["evidence_refs"] = ["new-target"]
    detail = attach_execution_proposal({"component_changes": [again]}, {}, context=frozen,
                                       allowed_modules=["prompt"], working_bundle={}, skill_changed=False)
    assert detail["_execution_detail"]["component_changes"][0]["evidence_refs"] == ["old-target", "new-target"]


def test_unknown_component_still_uses_original_strict_validation():
    with pytest.raises(SkillLibraryError, match="unknown"):
        attach_execution_proposal({"component_changes": [component_change(component_id="unknown")]}, {},
                                  context=context(), allowed_modules=["prompt"], working_bundle={}, skill_changed=False)


def test_synthesis_receives_safe_metrics_without_pending_candidate_or_reserved_ids():
    frozen = pending_context(context(), candidate())
    frozen.update(active_candidate_job_id="pending-id", excluded_source_keys=["LOCKED-family"])
    value = synthesis_context(frozen)
    assert value["active_candidate_feedback"]["candidate"]["total_tokens"] == 12
    assert not {"working_execution_plan", "inherited_component_changes", "active_candidate",
                "active_candidate_job_id", "excluded_source_keys"} & value.keys()
    assert "Pending A" not in json.dumps(value) and "LOCKED" not in json.dumps(value)
    assert "working_execution_plan" in frozen


class Branches:
    def __init__(self):
        self.saved = None
        self.writes = 0
        self.key = None
        self.validation = None
        self.command_reads = 0

    async def branch(self, *args):
        return copy.deepcopy(self.saved)

    async def command(self, *args):
        self.command_reads += 1
        return copy.deepcopy(self.validation)

    async def save_branch(self, *args, result, **kwargs):
        self.saved = copy.deepcopy(result)
        self.writes += 1
        self.key = kwargs["key"]


@pytest.mark.asyncio
async def test_retry_reuses_exact_pre_model_snapshot_even_if_feedback_or_history_grows():
    repo, live = Branches(), candidate()
    task = SimpleNamespace(task_id="evolve-one", tenant_id="default")
    first = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                        context=context(), candidate=live, sessions=[{
                                            "session_id": "new", "_skills_referenced": {"demo"},
                                            "_skills_injected": set(),
                                        }])
    json.dumps(repo.saved)  # The real metadata extractor adds sets; the PG checkpoint must remain JSON.
    assert first["sessions"][0]["_skills_referenced"] == {"demo"}
    assert first["sessions"][0]["_skills_injected"] == set()
    assert repo.key == "__prompt_planning__:execution:instructor"
    live["evaluation"]["replay"]["efficiency"]["candidate"]["total_tokens"] = 999
    second = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                         context=context(), candidate=live, sessions=[{"session_id": "later"}])
    assert first == second and repo.writes == 1
    for changed in ("candidate_id", "revision"):
        edited = copy.deepcopy(live)
        edited[changed] = "other" if changed == "candidate_id" else 4
        with pytest.raises(CandidateStateError):
            assert_planning_current(first, edited, "published")
    with pytest.raises(CandidateStateError):
        assert_planning_current(first, live, "other-published")


def test_only_same_revision_feedback_and_no_validating_candidate_are_accepted():
    live = candidate()
    live["evaluation"]["candidate_revision"] = 2
    assert pending_context(context(), live)["active_candidate_feedback"] == {}
    live["status"] = "validating"
    with pytest.raises(CandidateStateError, match="finish validation"):
        pending_context(context(), live)


def test_existing_development_feedback_retains_positive_negative_and_multiturn_actual_results():
    live = candidate()
    command = development_validation(live)
    original = copy.deepcopy((live, command))
    feedback = validation_feedback(context(), live, command)
    assert feedback["candidate_revision"] == 3
    assert feedback["usage"] == "development_diagnostic_only"
    assert len(feedback["cases"]) == 1
    case = feedback["cases"][0]
    assert case["query"] == "Original development question"
    assert case["baseline"]["completed"] is True and case["candidate"]["completed"] is False
    assert case["baseline"]["checks"][0]["satisfied"] is True
    assert case["candidate"]["checks"][0] == {
        "id": "R01", "satisfied": False, "evidence": "candidate actual machine reason",
    }
    assert len(case["candidate"]["interactions"]) == 2
    text = json.dumps(feedback)
    assert not any(private in text for private in ("LOCKED", "RAW-PRIVATE", "PROBE-PRIVATE", "-case"))
    assert (live, command) == original


def external_development_validation(live, *, report_in_window=False):
    command = development_validation(live)
    evaluation = normalize_evaluation({"mode": "external_agent", "external_agent": {
        "url": "https://evaluator.example/score", "secret_ref": "sref_0123456789abcdef0123456789abcdef",
        "rubric": "Business quality",
    }})
    command["payload"]["replay_config"] = {"revision": 7, "config": {"evaluation": evaluation}}
    replay = live["evaluation"]["replay"]
    replay.update(evaluation=evaluation, evaluation_policy_hash=evaluation_policy_hash(evaluation))
    for case, pair in zip(command["payload"]["detail"]["replay_cases"], replay["cases"], strict=True):
        report = normalize_external_report({
            "baseline": {"score": 75, "reason": "Preserved the business intent"},
            "candidate": {"score": 60, "reason": "Added a business precondition absent from the original"},
        }, external_case_request(SimpleNamespace(evaluation=evaluation), case, pair))
        if report_in_window:
            replay.setdefault("window_results", {})[case["case_id"]] = {
                "case_id": case["case_id"], "evaluation": evaluation,
                "evaluation_policy_hash": evaluation_policy_hash(evaluation), "external_evaluation": report,
            }
        else:
            pair["external_evaluation"] = report
    refresh_validation(live, command)
    return command


def refresh_validation(live, command):
    from team_skills.evolution.repository import digest

    command["payload_hash"] = digest(command["payload"])
    command["result"]["evaluation"] = copy.deepcopy(live["evaluation"])


@pytest.mark.parametrize("report_in_window", [False, True])
def test_external_feedback_retains_real_scores_and_outputs_without_requiring_checklist_judge(report_in_window):
    live = candidate()
    command = external_development_validation(live, report_in_window=report_in_window)
    for pair in live["evaluation"]["replay"]["cases"]:
        for branch in ("baseline", "candidate"):
            pair[branch]["checklist_report"] = {"judge": "unavailable", "items": []}
    refresh_validation(live, command)
    before = copy.deepcopy((live, command))
    feedback = validation_feedback(context(), live, command)
    assert feedback["evaluation_mode"] == "external_agent"
    assert feedback["machine_judgment_is_not_business_authority"] is True
    assert len(feedback["cases"]) == 1  # Locked acceptance remains excluded.
    item = feedback["cases"][0]
    assert item["checks"][0]["text"] == "Preserve the original business intent"
    assert item["external_evaluation"]["candidate"] == {
        "score": 60, "reason": "Added a business precondition absent from the original",
    }
    assert item["candidate"]["checks"] == [] and len(item["candidate"]["interactions"]) == 2
    assert item["candidate"]["completed"] is False
    assert not any(private in json.dumps(feedback) for private in (
        "LOCKED", "RAW-PRIVATE", "sref_0123456789abcdef0123456789abcdef", "https://evaluator.example",
    ))
    assert (live, command) == before


@pytest.mark.parametrize("change", ["frozen_mode", "policy_hash", "request_hash", "actual_output", "score"])
def test_external_feedback_requires_the_frozen_policy_and_exact_scored_execution(change):
    live = candidate()
    command = external_development_validation(live)
    replay = live["evaluation"]["replay"]
    pair = replay["cases"][0]
    if change == "frozen_mode":
        command["payload"]["replay_config"]["config"]["evaluation"] = normalize_evaluation(None)
    elif change == "policy_hash":
        replay["evaluation_policy_hash"] = "another-policy"
    elif change == "request_hash":
        pair["external_evaluation"]["request_hash"] = "another-request"
    elif change == "actual_output":
        pair["candidate"]["interactions"][0]["response"] = "An unscored changed answer"
    else:
        pair["external_evaluation"]["candidate"]["score"] = True
    refresh_validation(live, command)
    assert validation_feedback(context(), live, command) == {}


@pytest.mark.asyncio
async def test_external_feedback_freezes_once_drives_writer_and_stays_out_of_case_synthesis():
    repo, live = Branches(), candidate()
    repo.validation = external_development_validation(live)
    assert efficiency_feedback(live["evaluation"]) == {}
    task = SimpleNamespace(task_id="external-feedback-evolve", tenant_id="default")
    first = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                        context=context(), candidate=live, sessions=[])
    frozen = first["execution_context"]
    assert frozen["active_candidate_feedback"] == {}
    assert frozen["active_candidate_replay_feedback"]["evaluation_mode"] == "external_agent"
    system, body = prompt_editor_input([], frozen)
    assert body["active_candidate_feedback"] == {}
    assert body["active_candidate_replay_feedback"] == frozen["active_candidate_replay_feedback"]
    assert "它替代效率作为本轮评价信号" in system
    synthesis = synthesis_context(frozen)
    assert "active_candidate_replay_feedback" not in synthesis and synthesis["active_candidate_feedback"] == {}
    assert "original actual response" not in json.dumps(synthesis)
    assert "Added a business precondition" not in json.dumps(synthesis)
    repo.validation = None
    second = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                         context=context(), candidate=live, sessions=[])
    assert first == second and repo.command_reads == 1 and repo.writes == 1


def test_native_frozen_plan_normalization_keeps_feedback_but_different_actual_plan_is_rejected():
    from team_replay.execution_plan import execution_plan_hash
    from team_skills.evolution.repository import digest

    live = candidate()
    command = development_validation(live)
    before = copy.deepcopy((live, command))
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        assert type(live["detail"][key]["components"][0]["revision"]) is int
        assert type(command["payload"]["detail"][key]["components"][0]["revision"]) is str
        assert execution_plan_hash(live["detail"][key]) == execution_plan_hash(command["payload"]["detail"][key])
    assert len(validation_feedback(context(), live, command)["cases"]) == 1
    assert (live, command) == before
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        changed = copy.deepcopy(command)
        changed["payload"]["detail"][key]["environment"]["request"]["temperature"] = 0.7
        changed["payload_hash"] = digest(changed["payload"])
        assert validation_feedback(context(), live, changed) == {}


@pytest.mark.parametrize("change", [
    "unfinished", "payload_hash", "old_revision", "other_candidate", "other_profile", "other_base",
    "changed_result", "changed_plan", "unknown_selection", "probe", "unverified", "wrong_request",
    "wrong_actual_plan", "changed_check", "unknown_judge",
])
def test_feedback_with_stale_or_private_or_unverified_origin_is_not_training_material(change):
    from team_skills.evolution.repository import digest

    live = candidate()
    command = development_validation(live)
    pair = live["evaluation"]["replay"]["cases"][0]
    detail = command["payload"]["detail"]
    if change == "unfinished":
        command["finished_at"] = None
    elif change == "old_revision":
        live["evaluation"]["candidate_revision"] = 2
    elif change == "other_candidate":
        command["payload"]["candidate_id"] = "someone-else"
    elif change == "other_profile":
        detail["execution_profile_id"] = "another-profile"
    elif change == "other_base":
        detail["base_release_id"] = "another-release"
    elif change == "changed_result":
        command["result"]["evaluation"] = {}
    elif change == "changed_plan":
        detail["candidate_execution_plan"]["components"][0]["content"] = "changed"
    elif change == "unknown_selection":
        pair.pop("selection")
    elif change == "probe":
        pair["diagnostic_kind"] = "baseline_baseline"
    elif change == "unverified":
        pair["candidate"]["execution_verification"] = "pending"
    elif change == "wrong_request":
        pair["candidate"]["interactions"][0]["prompt"] = "Different question"
    elif change == "wrong_actual_plan":
        pair["candidate"]["execution_plan_hash"] = "wrong-plan"
    elif change == "changed_check":
        pair["candidate"]["checklist_report"]["items"][0]["text"] = "Changed original criterion"
    elif change == "unknown_judge":
        pair["candidate"]["checklist_report"]["judge"] = "unavailable"
    if change != "changed_result":
        command["result"]["evaluation"] = copy.deepcopy(live["evaluation"])
    command["payload_hash"] = "wrong-hash" if change == "payload_hash" else digest(command["payload"])
    assert validation_feedback(context(), live, command) == {}


@pytest.mark.asyncio
async def test_existing_completed_feedback_is_frozen_once_and_never_sent_to_casesynth():
    repo, live = Branches(), candidate()
    repo.validation = development_validation(live)
    task = SimpleNamespace(task_id="new-feedback-evolve", tenant_id="default")
    first = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                        context=context(), candidate=live, sessions=[])
    feedback = first["execution_context"]["active_candidate_replay_feedback"]
    assert len(feedback["cases"]) == 1 and repo.command_reads == 1
    assert "active_candidate_replay_feedback" not in synthesis_context(first["execution_context"])
    repo.validation = None
    second = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                         context=context(), candidate=live, sessions=[])
    assert first == second and repo.command_reads == 1 and repo.writes == 1
    # Pre-protocol snapshots remain historical: no backfill, no new command lookup.
    repo.saved.pop("replay_feedback_protocol")
    saved = copy.deepcopy(repo.saved)
    legacy = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                         context=context(), candidate=live, sessions=[])
    assert "active_candidate_replay_feedback" not in legacy["execution_context"]
    assert repo.saved == saved and repo.command_reads == 1 and repo.writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "row_missing", "different_archive_hash", "corrupt_payload"])
async def test_summary_history_only_recovers_exact_archived_original_outside_transaction(monkeypatch, failure):
    from session_ingestion.queries import SessionQueries
    from session_ingestion.schemas import session_payload_hash

    raw = analyzed_source()
    expected_hash = session_payload_hash(raw)
    history = {"session_id": raw["session_id"], "content_hash": expected_hash, "_summary": "Not original text"}
    row = {"session_id": raw["session_id"], "content_ref": "immutable-original", "content_hash": expected_hash,
           "user_id": "original-user"}
    active, reads = [], []

    class Tx:
        @asynccontextmanager
        async def tenant_tx(self, tenant):
            assert tenant == "default"
            active.append(True)
            try:
                yield object()
            finally:
                active.pop()

    async def get_session(self, conn, *, session_id):
        assert active and session_id == raw["session_id"]
        return None if failure == "row_missing" else {**row, **(
            {"content_hash": "newer-different-source"} if failure == "different_archive_hash" else {})}

    def read_payload(ref):
        assert not active and ref == "immutable-original"
        reads.append(ref)
        value = copy.deepcopy(raw)
        if failure == "corrupt_payload":
            value["turns"][0]["response_text"] = "tampered"
        return value

    monkeypatch.setattr(SessionQueries, "get_session", get_session)
    result, unavailable = await restore_prompt_history(
        SimpleNamespace(tx=Tx()), SimpleNamespace(tenant_id="default"), [history],
        SimpleNamespace(get_session=read_payload),
    )
    if failure:
        assert result == [] and unavailable == [raw["session_id"]]
    else:
        assert unavailable == [] and result[0]["turns"] == raw["turns"]
        assert result[0]["_summary"] == raw["_summary"] and result[0]["_evidence_window"] == "historical"
        assert result[0]["user_alias"] == "original-user"
    assert len(reads) == (failure not in {"row_missing", "different_archive_hash"})


@pytest.mark.asyncio
async def test_legacy_snapshot_private_feedback_stays_stored_but_is_not_consumed_or_sent_to_synthesis():
    repo, live = Branches(), candidate()
    task = SimpleNamespace(task_id="legacy-evolve", tenant_id="default")
    await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                 context=context(), candidate=live, sessions=[{"session_id": "new"}])
    retired = ("active_candidate_completion", "active_candidate_token_feedback", "active_candidate_diagnostic_context",
               "active_candidate_behavior_feedback", "replay_diagnostic_audit", "behavior_review_protocol")
    for key in retired:
        repo.saved[key] = {"private": "PRIVATE-RETIRED-FEEDBACK"}
        repo.saved["execution_context"][key] = {"private": "PRIVATE-RETIRED-FEEDBACK"}
    repo.saved["execution_context"]["active_candidate_feedback"]["candidate"].update(
        input_tokens=7, output_tokens=5, cases=["PRIVATE-RETIRED-FEEDBACK"],
    )
    original = copy.deepcopy(repo.saved)
    projected = await freeze_prompt_planning(None, repo, task=task, branch_key="execution:instructor",
                                             context=context(), candidate=live, sessions=[{"session_id": "later"}])
    assert repo.saved == original and repo.writes == 1
    assert not set(retired) & projected.keys()
    assert not set(retired) & projected["execution_context"].keys()
    assert projected["feedback_result_hash"] == original["feedback_result_hash"]
    assert projected["active_candidate"] == original["active_candidate"]
    assert projected["sessions"] == original["sessions"]
    feedback = projected["execution_context"]["active_candidate_feedback"]
    assert feedback == live["evaluation"]["replay"]["efficiency"]
    assert "PRIVATE-RETIRED-FEEDBACK" not in json.dumps(projected)
    assert "PRIVATE-RETIRED-FEEDBACK" not in json.dumps(synthesis_context(projected["execution_context"]))
    assert projected == await freeze_prompt_planning(
        None, repo, task=task, branch_key="execution:instructor", context=context(), candidate=live, sessions=[],
    )
    assert repo.saved == original and repo.writes == 1
