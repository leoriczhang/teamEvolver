"""Public business-input fixtures with declared offline request reconstruction."""
from __future__ import annotations

import copy
import json

from team_replay.execution_inputs import EXECUTION_INPUTS_SCHEMA, TEMPLATE_INPUT_CONTRACT_SCHEMA
from team_replay.execution_plan import render_components
from team_skills.execution.contracts import plan_for, validate_profile

from .execution import profile_raw

VARIABLE_NAMES = ["sceneBackground", "userProfile", "userPersonality", "aiTask"]
USER_TEMPLATE = ('对话历史：{{chatHistory}}（以json字符串数组格式表示，一个数组元素就是一句话；'
                 '其中 “HWP：” “C：” 的前缀分别表示对话场景中的双方）\n\n请回复')


def shared_template_profile():
    raw = profile_raw("shared-customer")
    raw["affected_skill_ids"] = []
    raw["components"] = [{"component_id": "main-prompt", "kind": "agent_prompt", "slot": "system", "order": 0,
                          "content": ("模拟客户 {{userProfile}}。背景：{{sceneBackground}}。"
                                      "性格：{{userPersonality}}。任务：{{aiTask}}。只能回复客户的话。") }]
    raw["environment"].update({
        "template_variables": dict.fromkeys(VARIABLE_NAMES, "Default unused value"),
        "template_input_contract": {"schema_version": TEMPLATE_INPUT_CONTRACT_SCHEMA,
                                    "variable_names": VARIABLE_NAMES},
    })
    profile = validate_profile(raw)
    profile["components"][0]["binding"]["template_id"] = "business-customer-template"
    return profile


def shared_template_plan():
    profile = shared_template_profile()
    return plan_for(profile, profile["components"])


def shared_template_source(index=1, *, plan=None):
    plan = plan or shared_template_plan()
    variables = {"sceneBackground": f"电话场景{index}", "userProfile": f"客户{index}",
                 "userPersonality": "谨慎", "aiTask": "收到邀约先拒绝" if index % 2 else ""}
    history = json.dumps([f"HWP：你好，客户{index}", "C：你好。"], ensure_ascii=False)
    public_inputs = [{"role": "user", "content": USER_TEMPLATE.replace("{{chatHistory}}", history)}]
    query = f"  HWP：这周三方便见面吗，客户{index}？\r\n"
    component = plan["components"][0]
    source_inputs = {"schema_version": EXECUTION_INPUTS_SCHEMA,
                     "template_refs": {component["component_id"]: {
                         "template_id": component["binding"]["template_id"],
                         "revision": component["revision"], "content_hash": component["content_hash"]}},
                     "template_variables": variables, "input_messages": public_inputs}
    effective = copy.deepcopy(plan)
    effective["environment"]["template_variables"] = copy.deepcopy(variables)
    full_messages = [{"role": part["role"], "content": part["content"]} for part in render_components(effective)]
    full_messages += copy.deepcopy(public_inputs) + [{"role": "user", "content": query}]
    ref = {"source_session_key": f"root-{index}", "trace_id": f"trace-{index}",
           "revision": "fact-r1", "context_revision": "context-r1"}
    turn = {
        "turn_num": 1, "source_trace_id": ref["trace_id"], "prompt_text": query,
        "response_text": "PRIVATE_OLD_ANSWER", "messages": [
            {"role": "user", "content": query}, {"role": "assistant", "content": "PRIVATE_OLD_ANSWER"},
            {"role": "user", "content": "PRIVATE_OLD_GRADE"}],
        "request_context": {"evidence_level": "generation_input", "completeness": "unknown",
            "generation_requests": [{"observation_id": f"generation-{index}", "input": {"messages": full_messages}}],
            "trace_input": {"messages": [{"role": "user", "content": query}], "template_inputs": source_inputs,
                "reconstruction": {"constructed_import_batch": True, "actual_original_request_observed": False,
                                   "original_session_independence": "unknown"}}},
    }
    return {
        "session_id": f"source-{index}", "user_alias": f"user-{index}", "used_skills": [], "turns": [turn],
        "runtime": {"type": plan["binding"]["runtime_type"]},
        "runtime_context": {"agent_id": plan["binding"]["agent_id"],
                            "execution_profile_id": plan["binding"]["profile_id"]},
        "provenance": {"schema_version": "session-source.v1", "source_session_key": ref["source_session_key"],
                       "identity_verified": True, "trace_identity_verified": True,
                       "target_trace_refs": [ref], "trace_revisions": [ref], "context_trace_refs": []},
    }


def shared_source_cases(packet):
    return {"test_datasets": [{
        "name": f"Business source {index}", "query_mode": "recorded_anchor", "anchor_id": example["example_id"],
        "source_session_ids": ["source-1", "source-2"], "evidence_window": "recent",
        "checks": [{"id": f"R{i:02d}", "kind": "output", "text": f"Original observable requirement {i}"}
                   for i in range(1, 8)],
    } for index, example in enumerate(packet["examples"])]}
