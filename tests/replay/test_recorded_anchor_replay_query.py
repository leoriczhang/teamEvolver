"""A frozen source query must reach the observed transport without trimming.

All transports and Checklist judgments here are synthetic. No Agent or model is
called; the production materialization, Replay and receipt implementations run.
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.datasets.recorded_anchor import materialize_recorded_anchor_queries, recorded_anchor_review_bindings
from team_replay.datasets.schema import normalize_case
from team_replay.engine import annotate_cases, make_context
from team_replay.execution import run_branch
from team_skills.execution.contracts import plan_for, validate_profile
from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context
from tests.support.execution import profile_raw


def _case(query):
    source = {"session_id": "source", "turns": [{"source_trace_id": "target", "prompt_text": query,
        "response_text": "Original historical response", "request_context": {"trace_input": {"reconstruction": {
            "constructed_import_batch": True, "original_session_independence": "unknown",
            "actual_original_request_observed": False,
        }}}}], "provenance": {"target_trace_refs": [{"trace_id": "target"}], "context_trace_refs": []}}
    packet = build_evidence_context([source])
    checks = [{"id": f"R{i}", "kind": "output", "text": f"Synthetic protocol check {i}"} for i in range(6)]
    response = {"test_datasets": [{"query_mode": "recorded_anchor", "name": "Bound input",
        "anchor_id": packet["examples"][0]["example_id"], "source_session_ids": ["source"],
        "checks": checks, "evidence_window": "recent"}]}
    materialized, bindings = materialize_recorded_anchor_queries(
        response, evidence=packet, sessions=[source], enabled=True,
    )
    binding = bindings[0]
    anchor = {key: binding[key] for key in (
        "anchor_id", "session_id", "trace_id", "evidence_ref", "recorded_query_sha256", "evidence_context_sha256",
    )}
    case = normalize_case({"case_id": "bound-query", "query": materialized["test_datasets"][0]["query"],
        "checks": checks, "skill_ids": [], "provenance": {
            "evidence_anchor": anchor, "query_materialization": binding,
        }})
    assert case["query"] == query and recorded_anchor_review_bindings([case])
    return case


def _replay(case, *, trim_in_adapter=False):
    captured = []

    def post(endpoint, **options):
        assert endpoint == "https://model.invalid/synthetic-transport"
        captured.append(json.loads(options["content"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Synthetic response"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            request=httpx.Request("POST", endpoint))

    factory = ObservedHttpFactory({"endpoint": "https://model.invalid/synthetic-transport"}, post=post)
    if trim_in_adapter:
        opened = factory.open

        def open_with_mutation(context):
            session = opened(context)
            send = session.send
            session.send = lambda message: send(message.strip())
            return session

        factory.open = open_with_mutation
    annotated = annotate_cases({"replay_cases": [case]}, [])[0]
    profile = validate_profile(profile_raw())
    plan = plan_for(profile, profile["components"])
    context = make_context("baseline", None, annotated, {"runtime": {"type": "observed_http"}}, 30,
                           snapshot={}, execution_plan=plan)
    result = run_branch(factory, context, annotated, harness={}, max_interactions=1,
                        judge=lambda **_: {"judge": "synthetic_fixture", "all_satisfied": True, "items": []})
    return annotated, captured, result


@pytest.mark.parametrize("query", ["\r\n \t\u3000任务A\r\nUnicode é\u2028内容B\r\n\u00a0", "  request\t\r\n"])
def test_recorded_anchor_original_bytes_reach_the_actual_captured_transport(query):
    case = _case(query)
    before = copy.deepcopy(case)
    annotated, captured, result = _replay(case)
    assert result["execution_verification"] == "verified" and result["ok"]
    assert captured[0]["messages"][-1] == {"role": "user", "content": query}
    assert result["interactions"][0]["prompt"] == query
    assert annotated["query"] == query and recorded_anchor_review_bindings([annotated])
    assert case == before


def test_an_adapter_that_trims_a_bound_first_query_is_rejected_by_execution_receipts():
    query = "\r\n  Bound request\t\u3000"
    _, captured, result = _replay(_case(query), trim_in_adapter=True)
    assert captured[0]["messages"][-1]["content"] == query.strip()
    assert result["execution_verification"] != "verified" and not result["ok"]
    assert result["error_code"] == "REPLAY_UNSUPPORTED"


@pytest.mark.parametrize("mutation", ["query", "hash", "bytes", "anchor_ref", "anchor_context", "schema"])
def test_an_altered_recorded_binding_is_rejected_before_opening_any_adapter(mutation):
    case = _case("  Original query\r\n")
    binding = case["provenance"]["query_materialization"]
    if mutation == "query":
        case["query"] += "\nDifferent input"
    elif mutation == "hash":
        binding["recorded_query_sha256"] = "0" * 64
    elif mutation == "bytes":
        binding["recorded_query_utf8_bytes"] += 1
    elif mutation == "anchor_ref":
        case["provenance"]["evidence_anchor"]["evidence_ref"] = "wrong-source"
    elif mutation == "anchor_context":
        case["provenance"]["evidence_anchor"]["evidence_context_sha256"] = "0" * 64
    else:
        binding["schema_version"] = "unsupported-schema"
    with pytest.raises(EvidenceContextError):
        annotate_cases({"replay_cases": [case]}, [])


@pytest.mark.parametrize("mode", ["no_binding", "legacy_anchor", "other_mode"])
def test_legacy_and_other_modes_keep_the_existing_whitespace_normalization(mode):
    query = "\r\n  Legacy query\u3000\t"
    case = _case(query)
    if mode == "no_binding":
        case["provenance"] = {}
    elif mode == "legacy_anchor":
        case["provenance"].pop("query_materialization")
    else:
        case["provenance"]["query_materialization"]["query_mode"] = "future_other_mode"
    annotated, captured, result = _replay(case)
    assert annotated["query"] == query.strip()
    assert captured[0]["messages"][-1]["content"] == query.strip()
    assert result["ok"] and result["execution_verification"] == "verified"
