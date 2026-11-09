"""The Prompt writer receives the exact evidence whitelist enforced after generation."""

from __future__ import annotations

import copy
import json

import pytest

from team_skills.evolution.kernel.enums import DecisionAction
from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.editor import EDITOR_PROTOCOL
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.planner import CONTEXT_INSTRUCTIONS, SYSTEM, propose_prompt
from team_skills.execution.planning import pending_context, validation_feedback
from team_skills.library.repository import SkillLibraryError
from teamEvolver.evidence_context import EvidenceContextError
from tests.support.execution import component_change, profile_raw
from tests.support.execution_feedback import development_validation
from tests.support.prompt_editor import PromptEditorLLM as FakeWriter
from tests.support.prompt_editor import assert_editor_options, assert_successful_prompt_edit


def context():
    profile = validate_profile(profile_raw())
    return {"baseline_execution_plan": plan_for(profile, profile["components"])}


def sessions():
    target = {"source_trace_id": "target-a", "trace_id": "local-alias-a", "user_query": "New fact"}
    return [
        {"session_id": "formal-a", "provenance": {
            "external_session_id": "upstream-a", "context_trace_refs": [{"trace_id": "context-a"}],
            "target_trace_refs": [{"trace_id": "target-a"}],
        }, "turns": [{"trace_id": "context-a"}, target], "_evidence_turns": [target, target]},
        {"session_id": "formal-b", "turns": [{"trace_id": "target-b"}, target]},
        {"session_id": "formal-a", "turns": [{"trace_id": "target-b"}]},
    ]


class DelayedFinalWriter(FakeWriter):
    """Stage and mechanically validate a real patch, then inspect until an explicit final round."""

    def __init__(self, refs, *, final_round=None):
        super().__init__(refs)
        self.final_round = final_round

    async def chat(self, messages, **options):
        if len(self.editor_calls) < 4:
            return await super().chat(messages, **options)
        captured = (copy.deepcopy(messages), copy.deepcopy(options))
        self.calls.append(captured)
        self.editor_calls.append(captured)
        if self.final_round is not None and len(self.editor_calls) >= self.final_round:
            value = {"type": "final", "decision": {"action": "change_execution", "rationale": (
                "Submit the mechanically validated local patch after inspecting its unchanged source"
            )}}
        else:
            value = {"type": "tool_calls", "calls": [{"tool": "inspect_changes", "args": {}}]}
        return json.dumps(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds, expected_rounds", [(None, 12), (20, 20)])
async def test_prompt_round_budget_uses_the_shared_valid_patch_fallback(
    monkeypatch, max_rounds, expected_rounds,
):
    from team_skills.evolution.stages import execute

    monkeypatch.setattr(execute, "_agent_limits", lambda: {"max_rounds": 12, "max_tool_calls_per_round": 8})
    writer = DelayedFinalWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=context(), max_rounds=max_rounds)

    assert result["action"] == "change_execution"
    assert result["component_changes"][0]["content"].endswith("Clarify handling of the recorded correction.")
    assert result["editor_audit"]["mechanical_only"] is True
    assert len(writer.editor_calls) == expected_rounds
    assert len(writer.calls) == len(writer.editor_calls)
    assert [options["trace_metadata"]["round"] for _, options in writer.editor_calls] == list(
        range(1, expected_rounds + 1)
    )
    observation = json.loads(writer.editor_calls[-1][0][-1]["content"])
    assert observation["editor_state"]["max_rounds"] == expected_rounds
    assert observation["editor_state"]["applied_edits"] == 1
    assert isinstance(observation["editor_state"]["staged_hash"], str)
    assert "round_limit_notice" in observation


@pytest.mark.asyncio
async def test_late_prompt_final_and_round_fallback_materialize_the_same_validated_change(monkeypatch):
    from team_skills.evolution.stages import execute

    monkeypatch.setattr(execute, "_agent_limits", lambda: {"max_rounds": 12, "max_tool_calls_per_round": 8})
    evidence, frozen = sessions(), context()
    original = copy.deepcopy((evidence, frozen))
    default_writer = DelayedFinalWriter(["target-a"], final_round=13)
    default_result = await propose_prompt(default_writer, sessions=evidence, context=frozen)
    assert default_result["action"] == "change_execution"
    assert len(default_writer.editor_calls) == 12
    assert len(default_writer.calls) == len(default_writer.editor_calls)

    extended_writer = DelayedFinalWriter(["target-a"], final_round=13)
    result = await propose_prompt(extended_writer, sessions=evidence, context=frozen, max_rounds=20)
    assert result["action"] == "change_execution"
    assert result["component_changes"] == default_result["component_changes"]
    assert len(extended_writer.editor_calls) == 13
    assert len(extended_writer.calls) == len(extended_writer.editor_calls)
    assert result["component_changes"][0]["content"] == (
        extended_writer.component["content"] + "\nClarify handling of the recorded correction."
    )
    assert result["editor_audit"]["applied_edits"] == 1
    assert result["editor_audit"]["mechanical_only"] is True
    assert json.loads(extended_writer.editor_calls[-1][0][-1]["content"])["editor_state"]["max_rounds"] == 20
    assert execute._agent_limits()["max_rounds"] == 12
    assert (evidence, frozen) == original


@pytest.mark.asyncio
async def test_null_prompt_round_budget_inherits_the_current_skill_loop_budget(monkeypatch):
    from team_skills.evolution.stages import execute

    monkeypatch.setattr(execute, "_agent_limits", lambda: {"max_rounds": 7, "max_tool_calls_per_round": 8})
    writer = DelayedFinalWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=context(), max_rounds=None)
    assert result["action"] == "change_execution"
    assert len(writer.editor_calls) == 7
    assert execute._agent_limits()["max_rounds"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [-1, 0, 1, 65, True, False, "20", 20.0, [], {}])
async def test_invalid_prompt_round_budget_fails_before_editor_dispatch(invalid):
    writer = FakeWriter(["target-a"])
    with pytest.raises(SkillLibraryError, match="prompt_editor_max_rounds"):
        await propose_prompt(writer, sessions=sessions(), context=context(), max_rounds=invalid)
    assert writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("max_rounds, expected_rounds", [(2, 2), (64, 5)])
async def test_prompt_round_budget_accepts_both_inclusive_boundaries(max_rounds, expected_rounds):
    writer = FakeWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=context(), max_rounds=max_rounds)
    assert len(writer.editor_calls) == expected_rounds
    if max_rounds == 2:
        assert result["action"] == DecisionAction.SKIP
        assert len(writer.calls) == len(writer.editor_calls)
    else:
        assert_successful_prompt_edit(writer, result)


@pytest.mark.asyncio
async def test_native_skill_planner_prompt_round_override_does_not_change_skill_limits(monkeypatch):
    from team_skills.evolution.cycle import LLMSkillPlanner
    from team_skills.evolution.stages import execute

    monkeypatch.setattr(execute, "_agent_limits", lambda: {"max_rounds": 12, "max_tool_calls_per_round": 8})
    writer = DelayedFinalWriter(["target-a"], final_round=13)
    frozen = {**context(), "allowed_modules": ["prompt"]}
    result = await LLMSkillPlanner(writer)(
        name=None, sessions=sessions(), current=None, names=[], context=frozen,
        config={"prompt_editor_max_rounds": 20}, library_reader=None,
    )
    assert result["action"] == "change_execution"
    assert len(writer.editor_calls) == 13
    assert len(writer.calls) == len(writer.editor_calls)
    assert execute._agent_limits()["max_rounds"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("json_mode", [False, True])
async def test_provider_json_mode_is_opt_in_and_only_constrains_the_prompt_editor_protocol(json_mode):
    writer = FakeWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=context(), json_mode=json_mode)
    assert_successful_prompt_edit(writer, result)
    for _, options in writer.editor_calls:
        assert options.get("response_format") == ({"type": "json_object"} if json_mode else None)
    assert all(options["trace_name"] == "team-skill-evolver:prompt_edit" for _, options in writer.calls)
    assert result["editor_audit"]["mechanical_only"] is True


@pytest.mark.asyncio
async def test_invalid_provider_json_mode_fails_before_dispatch_instead_of_treating_a_string_as_enabled():
    writer = FakeWriter(["target-a"])
    with pytest.raises(SkillLibraryError, match="must be a boolean"):
        await propose_prompt(writer, sessions=sessions(), context=context(), json_mode="false")
    assert writer.calls == []


@pytest.mark.asyncio
async def test_writer_receives_ordered_deduplicated_exact_refs_and_retains_context_as_context():
    evidence, frozen = sessions(), context()
    original = copy.deepcopy((evidence, frozen))
    writer = FakeWriter(["formal-a", "target-a", "target-b"])
    result = await propose_prompt(writer, sessions=evidence, context=frozen)
    assert_successful_prompt_edit(writer, result)
    messages, options = writer.calls[0]
    body = json.loads(messages[1]["content"])
    assert body["allowed_evidence_refs"] == ["formal-a", "formal-b", "target-a", "target-b"]
    assert body["execution_plan"] == frozen["baseline_execution_plan"]
    assert body["evidence"][0]["turns"] == evidence[0]["_evidence_turns"]
    assert body["evidence"][0]["analysis_context"] == {"turns": evidence[0]["turns"], "context_only": True}
    assert {item["relationship"] for item in body["evidence_context"]["examples"]} == {"recorded_session_turn"}
    assert "逐字选择allowed_evidence_refs" in messages[0]["content"]
    assert_editor_options(options, source_session_count=len(evidence))
    assert (evidence, frozen) == original
    assert result["action"] == "change_execution"
    changed = apply_changes(frozen["baseline_execution_plan"], result["component_changes"], allowed_modules=["prompt"])
    assert changed["target_component_ids"] == ["main-prompt"]


@pytest.mark.asyncio
async def test_pending_plan_is_the_edit_basis_but_old_refs_and_hidden_feedback_do_not_become_new_evidence():
    frozen = context()
    old = {**component_change("Pending component"), "evidence_refs": ["old-context-ref"]}
    frozen.update(working_execution_plan=apply_changes(frozen["baseline_execution_plan"], [old],
                                                     allowed_modules=["prompt"]),
                  active_candidate={"candidate_id": "same", "revision": 7, "base_release_id": "published"},
                  active_candidate_feedback={"candidate": {"total_tokens": 12, "query": "LOCKED"},
                                             "baseline": {"total_tokens": 9}, "cases": ["LOCKED"]})
    for retired in ("active_candidate_completion", "active_candidate_token_feedback",
                    "active_candidate_diagnostic_context", "active_candidate_behavior_feedback",
                    "replay_diagnostic_audit", "behavior_review_protocol"):
        frozen[retired] = {"private": "LOCKED-RETIRED-FEEDBACK"}

    writer = FakeWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=frozen)
    assert_successful_prompt_edit(writer, result)
    body = json.loads(writer.calls[0][0][1]["content"])
    assert body["execution_plan"] == frozen["working_execution_plan"]
    assert body["baseline_execution_plan"] == frozen["baseline_execution_plan"]
    assert body["active_candidate"]["revision"] == 7
    assert body["active_candidate_feedback"]["candidate"]["total_tokens"] == 12
    assert "LOCKED" not in json.dumps(body)
    assert "old-context-ref" not in body["allowed_evidence_refs"]


@pytest.mark.asyncio
async def test_existing_replay_failures_reach_writer_as_context_without_new_evidence_or_model_stages():
    frozen = {**context(), "candidate_kind": "execution", "execution_profile_id": "instructor",
              "base_release_id": "published", "affected_skill_ids": [], "skill_dependencies": []}
    detail = attach_execution_proposal(
        {"component_changes": [component_change("Pending current Prompt")]}, {}, context=frozen,
        allowed_modules=["prompt"], working_bundle={}, skill_changed=False,
    )["_execution_detail"]
    live = {"candidate_id": "pending", "revision": 2, "status": "open", "detail": detail}
    command = development_validation(live)
    planning = pending_context(frozen, live)
    planning["active_candidate_replay_feedback"] = validation_feedback(frozen, live, command)
    writer = FakeWriter(["target-a"])
    result = await propose_prompt(writer, sessions=sessions(), context=planning)
    assert_successful_prompt_edit(writer, result)
    body = json.loads(writer.calls[0][0][1]["content"])
    feedback = body["active_candidate_replay_feedback"]
    assert feedback == planning["active_candidate_replay_feedback"]
    assert feedback["cases"][0]["candidate"]["checks"][0]["satisfied"] is False
    assert feedback["cases"][0]["baseline"]["checks"][0]["satisfied"] is True
    assert body["evidence"][0]["turns"] == sessions()[0]["_evidence_turns"]
    assert "Original development question" not in body["allowed_evidence_refs"]
    assert all(options["trace_name"] == "team-skill-evolver:prompt_edit" for _, options in writer.calls)
    assert not any(private in json.dumps(body) for private in ("LOCKED", "PROBE-PRIVATE", "RAW-PRIVATE"))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["context-a", "upstream-a", "local-alias-a", "formal-a/target-a", "unknown", ""])
async def test_context_upstream_composed_unknown_and_empty_refs_remain_rejected(invalid):
    writer = FakeWriter([invalid])
    result = await propose_prompt(writer, sessions=sessions(), context=context())
    assert result["action"] == DecisionAction.SKIP
    assert not result.get("component_changes")
    assert writer.plan_rejection[0]["tool"] == "(plan)"
    assert "evidence_refs" in writer.plan_rejection[0]["error"]
    assert len(writer.editor_calls) == 2 and len(writer.calls) == len(writer.editor_calls)


@pytest.mark.asyncio
async def test_explicit_empty_target_turns_do_not_fall_back_to_old_context():
    evidence = [{"session_id": "formal-only", "turns": [{"trace_id": "context-only"}], "_evidence_turns": []}]
    writer = FakeWriter(["context-only"])
    result = await propose_prompt(writer, sessions=evidence, context=context())
    assert result["action"] == DecisionAction.SKIP
    assert not result.get("component_changes")
    assert writer.plan_rejection[0]["tool"] == "(plan)"
    assert "evidence_refs" in writer.plan_rejection[0]["error"]
    assert len(writer.editor_calls) == 2 and len(writer.calls) == len(writer.editor_calls)
    assert json.loads(writer.calls[0][0][1]["content"])["allowed_evidence_refs"] == ["formal-only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_refs", [[], None])
async def test_empty_reference_lists_keep_existing_strict_component_contract(empty_refs):
    frozen = context()
    writer = FakeWriter(empty_refs)
    result = await propose_prompt(writer, sessions=sessions(), context=frozen)
    assert result["action"] == DecisionAction.SKIP
    assert not result.get("component_changes")
    assert writer.plan_rejection[0]["tool"] == "(plan)"
    assert "evidence_refs" in writer.plan_rejection[0]["error"]
    assert len(writer.editor_calls) == 2 and len(writer.calls) == len(writer.editor_calls)
    with pytest.raises(SkillLibraryError, match="requires original evidence_refs"):
        apply_changes(frozen["baseline_execution_plan"], [
            {**component_change(), "evidence_refs": empty_refs},
        ], allowed_modules=["prompt"])


@pytest.mark.asyncio
async def test_missing_baseline_still_skips_without_calling_writer():
    writer = FakeWriter(["formal-a"])
    result = await propose_prompt(writer, sessions=sessions(), context={})
    assert result == {"action": DecisionAction.SKIP, "rationale": "execution_profile_missing"}
    assert writer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["", (
    "条件：接到约定会面的请求。\n"
    "条件的消息列表：不方便见面，在电话里讲就可以了；我很忙，没时间。\n"
    "结束的消息列表：好吧，我一般中午1点左右有时间。"
)])
async def test_original_requirements_trigger_branches_and_list_meanings_reach_writer_unchanged(task):
    raw = profile_raw()
    original_prompt = (
        "场景：{{sceneBackground}}；人设：{{userProfile}}；性格：{{userPersonality}}。\n"
        "已有去重要求：生成前逐条比对历史所有C发言，语义相似必须重新生成。\n"
        "任务：{{aiTask}}\n"
        "未触发条件按普通规则；首次触发从条件列表选；本轮再次触发从结束列表选。\n"
        "必须保持所选内容基本含义不变；任务优先级与角色边界保持。"
    )
    variables = {"sceneBackground": "电话沟通", "userProfile": "客户，育有三岁幼儿",
                 "userPersonality": "外向健谈", "aiTask": task}
    raw["components"][0]["content"] = original_prompt
    raw["environment"]["template_variables"] = variables
    profile = validate_profile(raw)
    frozen = {"baseline_execution_plan": plan_for(profile, profile["components"])}
    evidence = [{"session_id": "formal-correction", "turns": [{
        "source_trace_id": "target-correction", "response_text": "旧答仍复述了历史客户的话。",
        "messages": [{"role": "user", "content": "原模型评分：去重未满足；其余业务要求保持。"}],
    }]}]
    original = copy.deepcopy((evidence, frozen))
    writer = FakeWriter(["target-correction"])

    result = await propose_prompt(writer, sessions=evidence, context=frozen)
    assert_successful_prompt_edit(writer, result)

    messages, options = writer.calls[0]
    body = json.loads(messages[1]["content"])
    assert body["execution_plan"] == original[1]["baseline_execution_plan"]
    assert body["execution_plan"]["components"][0]["content"] == original_prompt
    assert body["execution_plan"]["environment"]["template_variables"] == variables
    assert body["evidence"][0]["turns"] == evidence[0]["turns"]
    assert body["allowed_evidence_refs"] == ["formal-correction", "target-correction"]
    assert "原业务条件、本轮是否触发、首次/再次触发分支、消息列表的基本含义" in messages[0]["content"]
    assert_editor_options(options, source_session_count=len(evidence))
    assert (evidence, frozen) == original


def reconstructed_import_rows():
    evidence = [{"session_id": "constructed-import-container", "provenance": {
        "source_session_key": "import-root", "target_trace_refs": [{"trace_id": "row-first"},
                                                                   {"trace_id": "row-second"}],
    }, "turns": []}]
    for index, (trace_id, history) in enumerate((
        ("row-first", "HWP：在忙吗？\nC：现在还行。"), ("row-second", ""),
    ), 1):
        query = "历史：" + (history or "暂无历史对话") + "\n当前消息：您好，请问方便吗？"
        request = {"messages": [{"role": "user", "content": query}], "reconstruction": {
            "origin": "offline_historical_reconstruction", "actual_original_request_observed": False,
            "constructed_import_batch": True, "original_session_independence": "unknown",
            "batch_order_meaning": "Preparation order only; historical order is unknown",
        }}
        evidence[0]["turns"].append({
            "turn_num": index, "source_trace_id": trace_id, "prompt_text": query,
            "response_text": "你好，请问是哪位？",
            "messages": [{"role": "user", "content": query},
                         {"role": "assistant", "content": "你好，请问是哪位？"},
                         {"role": "user", "content": "model_eval_feedback: 原评价模型的记录，不是人类反馈。"}],
            "request_context": {"generation_requests": [{"observation_id": "gen-" + trace_id,
                "input": request, "completeness": "unknown", "evidence_level": "generation_input"}],
                "trace_input": {"business_context": {"ctx_chatHistory": history, "ctx_historyMessage": history},
                                "reconstruction": copy.deepcopy(request["reconstruction"])}},
        })
    return evidence


@pytest.mark.asyncio
async def test_independent_reconstructed_rows_keep_own_history_and_unknown_original_request_evidence():
    evidence = reconstructed_import_rows()
    original = copy.deepcopy(evidence)
    writer = FakeWriter(["row-second"])

    result = await propose_prompt(writer, sessions=evidence, context=context())
    assert_successful_prompt_edit(writer, result)

    messages, _ = writer.calls[0]
    body = json.loads(messages[1]["content"])
    assert body["evidence"][0]["turns"] == original[0]["turns"]
    second = body["evidence"][0]["turns"][1]
    assert second["request_context"]["trace_input"]["business_context"]["ctx_chatHistory"] == ""
    assert second["request_context"]["generation_requests"][0]["input"]["reconstruction"] == (
        original[0]["turns"][1]["request_context"]["generation_requests"][0]["input"]["reconstruction"]
    )
    assert "导入批次相邻、数组顺序或turn_num不证明真实跨轮关系" in messages[0]["content"]
    assert body["allowed_evidence_refs"] == ["constructed-import-container", "row-first", "row-second"]
    packet = body["evidence_context"]
    assert packet["schema_version"] == "te.evidence-context.v1"
    assert packet["coverage"]["expected_count"] == packet["coverage"]["included_count"] == 2
    assert {x["trace_id"] for x in packet["examples"]} == {"row-first", "row-second"}
    assert {x["relationship"] for x in packet["examples"]} == {"isolated_record"}
    assert all(x["request_observation"]["declarations"]["actual_original_request_observed"] is False
               for x in packet["examples"])
    assert all(x["request_observation"]["original_session_independence"] == "unknown" for x in packet["examples"])
    packet_text = json.dumps(packet, ensure_ascii=False)
    assert "你好，请问是哪位？" not in packet_text
    assert original[0]["turns"][0]["messages"][0]["content"] not in packet_text
    assert "model_eval_feedback: 原评价模型的记录，不是人类反馈。" not in packet_text
    assert evidence == original


@pytest.mark.asyncio
async def test_conflicting_original_request_observation_stops_before_writer_call():
    evidence = reconstructed_import_rows()
    evidence[0]["turns"][1]["request_context"]["trace_input"]["reconstruction"].update(
        actual_original_request_observed=True,
    )
    original = copy.deepcopy(evidence)
    writer = FakeWriter(["row-second"])

    with pytest.raises(EvidenceContextError):
        await propose_prompt(writer, sessions=evidence, context=context())

    assert writer.calls == []
    assert evidence == original


def target_with_positive_context():
    evidence = reconstructed_import_rows()
    session = evidence[0]
    positive, target = copy.deepcopy(session["turns"])
    feedback = 'model_eval_feedback: 原评价模型，非人类反馈。{"综合得分":100,"综合判定":"PASS"}'
    positive["messages"][-1]["content"] = feedback
    positive["_langfuse"] = {"tags": ["machine-evaluation-feedback"]}
    refs = [{"source_session_key": "import-root", "trace_id": trace_id,
             "revision": "revision-" + trace_id, "context_revision": "context-" + trace_id}
            for trace_id in ("row-first", "row-second")]
    session.update(turns=[target], _evidence_turns=[target],
                   analysis_context={"context_only": True, "turns": [positive, target]})
    session["provenance"] = {"schema_version": "session-source.v1", "source_session_key": "import-root",
                             "identity_verified": True, "trace_identity_verified": True,
                             "target_trace_refs": [refs[1]], "context_trace_refs": [refs[0]],
                             "trace_revisions": refs}
    session["analysis_context"]["context_trace_refs"] = [copy.deepcopy(refs[0])]
    return evidence, positive, feedback


@pytest.mark.asyncio
async def test_full_positive_context_reaches_writer_without_becoming_a_target_or_verified_feedback():
    evidence, positive, feedback = target_with_positive_context()
    original = copy.deepcopy(evidence)
    writer = FakeWriter(["row-second"])

    result = await propose_prompt(writer, sessions=evidence, context=context())
    assert_successful_prompt_edit(writer, result)

    messages, options = writer.calls[0]
    body = json.loads(messages[1]["content"])
    packet = body["evidence_context"]
    assert len(packet["examples"]) == packet["coverage"]["expected_count"] == packet["coverage"]["included_count"] == 1
    assert [item["trace_id"] for item in packet["examples"]] == ["row-second"]
    assert len(packet["context_examples"]) == packet["coverage"]["context_count"] == 1
    control = packet["context_examples"][0]
    assert control["trace_id"] == "row-first" and control["usage"] == "context_only"
    assert control["recorded_query"] == positive["prompt_text"]
    assert control["recorded_messages"] == positive["messages"]
    assert control["recorded_turn"] == positive
    assert control["source_feedback"][0]["raw_text"] == feedback
    assert control["source_feedback"][0]["independently_verified"] is False
    assert body["allowed_evidence_refs"] == ["constructed-import-container", "row-second"]
    assert "row-first" not in {item["evidence_ref"] for item in packet["examples"]}
    assert packet["anchor_required_for_session_ids"] == ["constructed-import-container"]
    assert feedback not in json.dumps(body["evidence"], ensure_ascii=False)
    assert messages[0]["content"] == SYSTEM + CONTEXT_INSTRUCTIONS + EDITOR_PROTOCOL
    assert_editor_options(options, source_session_count=len(evidence))
    assert evidence == original


@pytest.mark.asyncio
async def test_context_trace_cannot_be_cited_even_when_its_full_positive_evaluation_is_visible():
    evidence, _, _ = target_with_positive_context()
    writer = FakeWriter(["row-first"])

    result = await propose_prompt(writer, sessions=evidence, context=context())
    assert result["action"] == DecisionAction.SKIP
    assert not result.get("component_changes")
    assert writer.plan_rejection[0]["tool"] == "(plan)"
    assert "evidence_refs" in writer.plan_rejection[0]["error"]
    assert len(writer.editor_calls) == 2 and len(writer.calls) == len(writer.editor_calls)

    body = json.loads(writer.calls[0][0][1]["content"])
    assert body["evidence_context"]["context_examples"][0]["trace_id"] == "row-first"
    assert body["allowed_evidence_refs"] == ["constructed-import-container", "row-second"]
