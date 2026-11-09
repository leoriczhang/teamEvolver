"""Shared templates retain source-specific inputs through validation and feedback."""
from __future__ import annotations

import copy

import pytest

from team_replay.execution_inputs import (
    EXECUTION_INPUTS_SCHEMA,
    TEMPLATE_INPUT_CONTRACT_SCHEMA,
    case_execution_plan_pair,
)
from team_replay.execution_plan import execution_plan_hash
from team_skills.evolution.repository import digest
from team_skills.execution import validation
from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.planning import validation_feedback
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change, profile_raw


def template_detail():
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    raw["components"] = raw["components"][:1]
    raw["components"][0]["content"] = "Service {{serviceName}}; limit {{limit}}; enabled {{enabled}}"
    raw["environment"].update(
        template_variables={"serviceName": "default", "limit": 2, "enabled": False},
        template_input_contract={"schema_version": TEMPLATE_INPUT_CONTRACT_SCHEMA,
                                 "variable_names": ["serviceName", "limit", "enabled"]},
    )
    profile = validate_profile(raw)
    baseline = plan_for(profile, profile["components"])
    candidate = apply_changes(baseline, [component_change(
        baseline["components"][0]["content"] + "; use concise replies",
    )], allowed_modules=["prompt"])
    baseline["target_component_ids"] = candidate["target_component_ids"]
    component = baseline["components"][0]
    cases = []
    for index in (1, 2):
        inputs = {"schema_version": EXECUTION_INPUTS_SCHEMA,
                  "source_ref": {"session_id": f"session-{index}", "source_session_key": f"root-{index}",
                                 "trace_id": f"trace-{index}", "revision": "source-v1"},
                  "template_refs": {component["component_id"]: {
                      "template_id": component["component_id"], "revision": component["revision"],
                      "content_hash": component["content_hash"]}},
                  "template_variables": {"serviceName": f"service-{index}", "limit": index, "enabled": True},
                  "input_messages": [{"role": "user", "content": f"Previous context {index}"}]}
        cases.append({"case_id": f"case-{index}", "query": "Give a status update", "skill_ids": [],
                      "checks": [{"id": "R1", "kind": "output", "text": "A concise status update"}],
                      "provenance": {"execution_inputs": inputs, "replay_selection": "current"}})
    return {"candidate_kind": "execution", "execution_profile_id": profile["profile_id"],
            "base_release_id": "published", "affected_skill_ids": [], "skill_dependencies": [],
            "allowed_modules": ["prompt"], "actual_modules": ["prompt"],
            "baseline_execution_plan": baseline, "candidate_execution_plan": candidate, "replay_cases": cases}


async def freeze(monkeypatch, detail):
    class Execution:
        def __init__(self, tx):
            pass

        async def current(self, conn, identity):
            return {"current_release_id": "published", "affected_skill_ids": []}, {}

    monkeypatch.setattr(validation, "ExecutionRepository", Execution)
    return await validation.freeze_execution_validation(None, None, detail)


def observed(detail):
    pairs = []
    for case in detail["replay_cases"]:
        plans = case_execution_plan_pair(detail["baseline_execution_plan"], detail["candidate_execution_plan"], case)
        pair = {"case_id": case["case_id"], "query": case["query"], "selection": "current",
                "provenance": copy.deepcopy(case["provenance"])}
        for branch, plan in zip(("baseline", "candidate"), plans, strict=True):
            pair[branch] = {"ok": True, "completed": True, "execution_verification": "verified",
                            "execution_plan_hash": execution_plan_hash(plan), "observed_requests": [{}],
                            "interactions": [{"prompt": case["query"], "response": "Service is available"}],
                            "checklist_report": {"items": [{**case["checks"][0], "satisfied": True,
                                                             "evidence": "Observed status reply"}]}}
        pairs.append(pair)
    return {"cases": pairs}


def test_registration_derives_stable_template_ids_only_for_explicit_shared_contract():
    detail = template_detail()
    binding = detail["baseline_execution_plan"]["components"][0]["binding"]
    assert binding["template_id"] == "main-prompt"
    legacy = validate_profile(profile_raw())
    assert all("template_id" not in part["binding"] for part in legacy["components"])


@pytest.mark.parametrize("bad", [None, {}, {"serviceName": "missing defaults"}])
def test_registration_rejects_invalid_contract_or_incomplete_defaults(bad):
    raw = profile_raw()
    raw["environment"]["template_input_contract"] = bad
    with pytest.raises(SkillLibraryError):
        validate_profile(raw)


@pytest.mark.asyncio
async def test_validation_freezes_distinct_source_specific_plans_without_changing_global_assets(monkeypatch):
    detail = template_detail()
    original = copy.deepcopy(detail)
    frozen = await freeze(monkeypatch, detail)
    hashes = frozen["validation_plan"]["case_plan_hashes"]
    assert hashes["case-1"]["baseline"] != hashes["case-2"]["baseline"]
    assert hashes["case-1"]["candidate"] != hashes["case-2"]["candidate"]
    assert validation.execution_gate(observed(frozen), frozen) == {"status": "passed", "reasons": []}
    assert detail == original
    assert frozen["baseline_execution_plan"]["environment"]["template_variables"]["serviceName"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["global_hash", "variables", "missing_case", "duplicate_case", "binding_hash"])
async def test_validation_rejects_cross_case_reuse_and_mutated_frozen_inputs(monkeypatch, change):
    detail = await freeze(monkeypatch, template_detail())
    result = observed(detail)
    if change == "global_hash":
        result["cases"][0]["baseline"]["execution_plan_hash"] = execution_plan_hash(detail["baseline_execution_plan"])
    elif change == "variables":
        detail["replay_cases"][0]["provenance"]["execution_inputs"]["template_variables"]["limit"] = 99
    elif change == "missing_case":
        result["cases"].pop()
    elif change == "duplicate_case":
        result["cases"][1] = copy.deepcopy(result["cases"][0])
    else:
        detail["validation_plan"]["case_plan_hashes"]["case-1"]["execution_inputs_hash"] = "changed"
    assert validation.execution_gate(result, detail)["status"] == "inconclusive"


@pytest.mark.asyncio
async def test_invalid_source_binding_is_a_business_validation_error(monkeypatch):
    detail = template_detail()
    del detail["replay_cases"][0]["provenance"]["execution_inputs"]
    with pytest.raises(SkillLibraryError, match="invalid Case execution inputs"):
        await freeze(monkeypatch, detail)


@pytest.mark.asyncio
async def test_feedback_uses_verified_case_inputs_without_promoting_source_identity_to_instructions(monkeypatch):
    detail = await freeze(monkeypatch, template_detail())
    result = observed(detail)
    evaluation = {"validation_run_id": "run-1", "candidate_revision": 7, "replay": result}
    candidate = {"candidate_id": "candidate-1", "revision": 7, "detail": detail, "evaluation": evaluation}
    payload = {"candidate_id": "candidate-1", "revision": 7, "detail": copy.deepcopy(detail)}
    command = {"kind": "skill.validate", "command_id": "run-1", "finished_at": "done", "payload": payload,
               "payload_hash": digest(payload), "result": {"evaluation": evaluation}}
    context = {"execution_profile_id": "instructor", "base_release_id": "published"}
    feedback = validation_feedback(context, candidate, command)
    assert len(feedback["cases"]) == 2
    first = feedback["cases"][0]["execution_inputs"]
    assert set(first) == {"template_variables", "input_messages"}
    assert first["template_variables"]["limit"] == 1
    command["payload"]["detail"]["validation_plan"]["case_plan_hashes"]["case-1"]["baseline"] = "tampered"
    command["payload_hash"] = digest(command["payload"])
    assert len(validation_feedback(context, candidate, command)["cases"]) == 1
