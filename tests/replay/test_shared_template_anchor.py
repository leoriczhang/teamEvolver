"""Native anchor Cases freeze the source inputs while preserving legacy model wire."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_replay.datasets import synthesis
from team_replay.datasets.recorded_anchor import materialize_recorded_anchor_queries, recorded_anchor_review_bindings
from team_replay.datasets.schema import make_document
from team_replay.execution_inputs import case_execution_plan_pair, normalize_execution_inputs, source_execution_inputs
from team_replay.execution_plan import ExecutionPlanError
from team_skills.evolution.replay_case_synthesis import CrossSessionCaseSynthesisError, LLMCrossSessionCaseSynthesizer
from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context
from teamEvolver.evidence_transport import SCHEMA, decode_evidence_transport
from tests.support.shared_template_sources import shared_source_cases, shared_template_plan, shared_template_source


def canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def source_packet():
    sources = [shared_template_source(1), shared_template_source(2)]
    packet = build_evidence_context(sources)
    return sources, packet, shared_source_cases(packet)


class NativeCaseModel:
    def __init__(self):
        self.responses = []

    async def chat(self, messages, **options):
        payload = json.loads(messages[1]["content"])
        if payload.get("schema") == SCHEMA:
            payload = decode_evidence_transport(payload)
        response = json.dumps(shared_source_cases(payload["evidence_context"]), ensure_ascii=False)
        self.responses.append(response)
        return response


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch):
    monkeypatch.setattr(synthesis, "_PROMPT_PROVIDER", None)
    monkeypatch.setattr(synthesis, "_OPTIONS_PROVIDER", None)


@pytest.mark.asyncio
async def test_single_native_synthesis_freezes_each_targets_own_inputs_and_immutable_receipt():
    sources, _, _ = source_packet()
    baseline, model = shared_template_plan(), NativeCaseModel()
    before = copy.deepcopy(sources)
    cases = await LLMCrossSessionCaseSynthesizer(model)(
        skill_name=None, sessions=sources, candidate_skill={}, evidence_context={
            "allowed_modules": ["prompt"], "execution_profile_id": baseline["binding"]["profile_id"],
            "baseline_execution_plan": baseline,
        },
    )
    assert len(cases) == 2 and len(model.responses) == 1 and sources == before
    for index, case in enumerate(cases):
        provenance = case["provenance"]
        inputs = provenance["execution_inputs"]
        source = sources[index]
        assert inputs == source_execution_inputs(source, source["turns"][0])
        assert case["query"] == source["turns"][0]["prompt_text"]
        assert inputs["source_ref"]["trace_id"] == f"trace-{index + 1}"
        assert provenance["query_materialization"]["execution_inputs_sha256"] == canonical_hash(inputs)
        receipt = provenance["query_materialization_receipt"]
        assert receipt["raw_generation_response"] == model.responses[0]
        assert "query" not in receipt["raw_case"] and "execution_inputs" not in receipt["raw_case"]
        assert case["checks"] == receipt["raw_case"]["checks"]
        assert "PRIVATE_OLD" not in json.dumps(provenance["session_snapshot"])
        assert normalize_execution_inputs(inputs) == inputs
        left, right = case_execution_plan_pair(baseline, baseline, case)
        assert left == right
        assert left["environment"]["template_variables"] == inputs["template_variables"]
        assert left["environment"]["case_input_messages"] == inputs["input_messages"]
        assert recorded_anchor_review_bindings([case])[0]["execution_inputs_sha256"] == canonical_hash(inputs)
        changed = copy.deepcopy(case)
        changed["provenance"]["execution_inputs"]["template_variables"]["aiTask"] = "forged"
        with pytest.raises(EvidenceContextError, match="input bindings"):
            recorded_anchor_review_bindings([changed])
        with pytest.raises(ExecutionPlanError, match="frozen source"):
            case_execution_plan_pair(baseline, baseline, changed)


@pytest.mark.parametrize("path", ["top", "provenance"])
def test_model_cannot_submit_its_own_variables_or_execution_input_binding(path):
    sources, packet, raw = source_packet()
    destination = raw["test_datasets"][0]
    if path == "provenance":
        destination = destination.setdefault("provenance", {})
    destination["execution_inputs"] = {"template_variables": {"aiTask": "forged"}}
    with pytest.raises(EvidenceContextError, match="Model-supplied"):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sources, enabled=True)


def test_rehashing_packet_cannot_replace_original_readonly_variables():
    sources, packet, raw = source_packet()
    packet["examples"][0]["recorded_context"]["template_inputs"]["template_variables"]["aiTask"] = "forged"
    packet["coverage"].pop("context_sha256")
    packet["coverage"]["context_sha256"] = canonical_hash(packet)
    with pytest.raises(EvidenceContextError, match="template inputs differ"):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sources, enabled=True)


def test_context_only_source_cannot_supply_shared_template_anchor():
    sources, packet, raw = source_packet()
    sources[0]["turns"][0]["context_only"] = True
    with pytest.raises(EvidenceContextError, match="Context-only"):
        materialize_recorded_anchor_queries(raw, evidence=packet, sessions=sources, enabled=True)


@pytest.mark.asyncio
async def test_wrong_original_generation_prevents_case_binding_despite_valid_input_metadata():
    sources, _, _ = source_packet()
    sources[0]["turns"][0]["request_context"]["generation_requests"][0]["input"]["messages"][0]["content"] += "forged"
    baseline = shared_template_plan()
    with pytest.raises(CrossSessionCaseSynthesisError, match="not verified"):
        await LLMCrossSessionCaseSynthesizer(NativeCaseModel())(
            skill_name=None, sessions=sources, candidate_skill={}, evidence_context={
                "allowed_modules": ["prompt"], "baseline_execution_plan": baseline,
            },
        )


@pytest.mark.asyncio
async def test_direct_request_template_inputs_are_included_in_evidence_and_source_bound():
    sources, _, _ = source_packet()
    for source in sources:
        request = source["turns"][0]["request_context"]
        request["template_inputs"] = request["trace_input"].pop("template_inputs")
    cases = await LLMCrossSessionCaseSynthesizer(NativeCaseModel())(
        skill_name=None, sessions=sources, candidate_skill={}, evidence_context={
            "allowed_modules": ["prompt"], "baseline_execution_plan": shared_template_plan(),
        },
    )
    assert len(cases) == 2


@pytest.mark.asyncio
async def test_shared_source_cannot_silently_fall_back_to_unbound_legacy_replay():
    sources, _, _ = source_packet()
    with pytest.raises(CrossSessionCaseSynthesisError, match="frozen shared-template plan"):
        await LLMCrossSessionCaseSynthesizer(NativeCaseModel())(
            skill_name=None, sessions=sources, candidate_skill={}, evidence_context={"allowed_modules": ["prompt"]},
        )


@pytest.mark.asyncio
async def test_equal_current_queries_with_distinct_anchors_keep_distinct_stable_case_ids():
    sources = [shared_template_source(1), shared_template_source(2)]
    for source in sources:
        turn = source["turns"][0]
        turn["prompt_text"] = "Same current query"
        turn["messages"][0]["content"] = turn["prompt_text"]
        request = turn["request_context"]
        request["trace_input"]["messages"][0]["content"] = turn["prompt_text"]
        request["generation_requests"][0]["input"]["messages"][-1]["content"] = turn["prompt_text"]
    context = {"allowed_modules": ["prompt"], "baseline_execution_plan": shared_template_plan()}
    cases = await LLMCrossSessionCaseSynthesizer(NativeCaseModel())(
        skill_name=None, sessions=sources, candidate_skill={}, evidence_context=context,
    )
    repeated = await LLMCrossSessionCaseSynthesizer(NativeCaseModel())(
        skill_name=None, sessions=sources, candidate_skill={}, evidence_context=context,
    )
    assert cases[0]["query"] == cases[1]["query"]
    assert cases[0]["provenance"]["execution_inputs"]["template_variables"] != (
        cases[1]["provenance"]["execution_inputs"]["template_variables"]
    )
    assert len({case["case_id"] for case in cases}) == 2
    assert [case["case_id"] for case in repeated] == [case["case_id"] for case in cases]
    assert len(make_document(dataset_id="shared", name="Shared template", skills=[], cases=cases)["cases"]) == 2


def test_legacy_unanchored_case_ids_keep_the_original_hash_bytes():
    identity = ["example-skill", "Same current query", "source-1", "source-2"]
    expected = "synth-" + hashlib.sha256("\0".join(identity).encode()).hexdigest()[:20]
    assert synthesis._dataset_id(skill_name=identity[0], query=identity[1], source_session_ids=identity[2:]) == expected
