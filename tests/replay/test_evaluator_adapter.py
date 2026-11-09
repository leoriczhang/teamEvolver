"""The evaluator extension preserves HTTP policies and isolates trusted adapter code."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from team_replay.evaluation import (
    evaluation_policy_hash,
    external_case_request,
    normalize_evaluation,
    normalize_external_report,
)
from team_replay.evaluator_adapters import build_evaluator, validate_evaluator_source

SOURCE = '''EVALUATOR_ADAPTER = {"label": "Custom grader", "enabled": True}
def evaluate(request, config, *, secret=""):
    assert secret == "worker-only-test-secret"
    request["branches"]["baseline"]["final_response"] = "modified locally"
    config["rubric"] = "modified locally"
    return {"baseline": {"score": 0, "reason": "Original error"},
            "candidate": {"score": 1, "reason": "Corrected answer"}}
'''


def reference(source=SOURCE, revision=1):
    return {"file": "custom.py", "revision": revision, "source_hash": hashlib.sha256(source.encode()).hexdigest()}


def test_adapter_reference_is_part_of_policy_without_changing_existing_http_hash():
    http = normalize_evaluation({"mode": "external_agent", "external_agent": {"url": "http://grader/score"}})
    assert "adapter" not in http["external_agent"]
    old_hash = evaluation_policy_hash(http)
    assert evaluation_policy_hash({**http, "external_agent": {**http["external_agent"], "adapter": None}}) == old_hash
    adapted = normalize_evaluation({"mode": "external_agent", "external_agent": {"adapter": reference()}})
    assert adapted["external_agent"]["url"] == ""
    assert evaluation_policy_hash(adapted) != old_hash
    second = copy.deepcopy(adapted)
    second["external_agent"]["adapter"]["revision"] = 2
    assert evaluation_policy_hash(second) != evaluation_policy_hash(adapted)
    case = {"case_id": "case-1", "query": "same question"}
    outputs = {"baseline": {"final_response": "Original"}, "candidate": {"final_response": "Candidate"}}
    old_request = external_case_request(SimpleNamespace(evaluation=adapted), case, outputs)
    new_request = external_case_request(SimpleNamespace(evaluation=second), case, outputs)
    assert old_request["request_id"] != new_request["request_id"]
    assert "secret_ref" not in str(old_request) and "evaluator_adapter" not in old_request


@pytest.mark.parametrize("change", [
    {"file": "../custom.py"}, {"revision": True}, {"revision": 0}, {"source_hash": "latest"},
    {"code": "no executable source in configuration"},
])
def test_bad_references_cannot_enter_saved_policy(change):
    with pytest.raises(ValueError, match="requires file"):
        normalize_evaluation({"mode": "external_agent", "external_agent": {"adapter": {**reference(), **change}}})


def test_source_validation_does_not_execute_code_and_runtime_copies_inputs():
    poisonous = SOURCE + '\nraise RuntimeError("must not run on API")\n'
    assert validate_evaluator_source(poisonous, "custom.py")["enabled"] is True
    with pytest.raises(ValueError, match="could not be loaded"):
        build_evaluator(poisonous, "custom.py", {}, tenant_id="alpha")
    config = {"rubric": "Original scoring rule"}
    request = {"request_id": "test", "branches": {"baseline": {"final_response": "Actual original reply"}}}
    original = copy.deepcopy(request)
    evaluator = build_evaluator(SOURCE, "custom.py", config, tenant_id="alpha", secret="worker-only-test-secret")
    report = normalize_external_report(evaluator.evaluate(request), request)
    assert report["candidate"]["score"] == 1
    assert request == original and config["rubric"] == "Original scoring rule"
    assert "worker-only-test-secret" not in repr(evaluator)


def test_provider_errors_are_redacted_and_invalid_adapter_scores_stay_unavailable():
    source = SOURCE[:SOURCE.index("def evaluate")] + '''def evaluate(request, config, *, secret=""):
    raise RuntimeError("private token=" + secret)
'''
    evaluator = build_evaluator(source, "custom.py", {}, tenant_id="a", secret="private-test-secret")
    with pytest.raises(RuntimeError) as error:
        evaluator.evaluate({})
    assert str(error.value) == "external evaluator adapter execution failed"
    with pytest.raises(ValueError):
        normalize_external_report({"baseline": {"score": 0, "reason": "No match"},
                                   "candidate": {"score": "1", "reason": "Invalid score"}}, {"request_id": "a"})
