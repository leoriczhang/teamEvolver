"""Document Cases use the real transport contract without historical Sessions."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from team_replay import engine
from team_replay.artifacts import (
    artifact_tree_sha256,
    decode_bundle_payload,
    document_execution_treatment,
    validate_skill_treatment,
)
from team_replay.contracts import (
    DOCUMENT_REPLAY_RUNTIME,
    ReplayContractError,
    document_replay_execution,
    document_runtime_type,
    skill_replay_spec,
)
from team_replay.datasets.schema import make_document
from team_replay.execution import run_branch
from team_replay.factories import TurnBasedReplayFactory
from team_replay.gateway import EmbeddedReplayGateway
from team_replay.host import configure_host, current_host
from team_replay.protocol import REPLAY_TURN_RESULT_SCHEMA
from team_replay.runtime_matrix import prepare_runtime_validation, runtime_type_for_case

HIDDEN_REQUIREMENT = "Include the verification receipt in the final report"


def _skill(content="Candidate method"):
    files = {
        "SKILL.md": content.encode(),
        "references/rules.md": b"Read the provided source and verify each shipment.",
        "scripts/verify.py": b"def verify(value): return value\n",
        "benchmark.json": HIDDEN_REQUIREMENT.encode(),
        "BENCHMARK.md": HIDDEN_REQUIREMENT.encode(),
        "EVALUATION.md": HIDDEN_REQUIREMENT.encode(),
    }
    return {
        "name": "document-method",
        "content": content,
        "bundle": {
            "format": "skill-bundle",
            "tree_sha256": artifact_tree_sha256(files),
            "files": [
                {
                    "path": path,
                    "encoding": "base64",
                    "content": base64.b64encode(value).decode(),
                    "size": len(value),
                    "sha256": hashlib.sha256(value).hexdigest(),
                }
                for path, value in files.items()
            ],
        },
    }


def _case():
    return make_document(
        dataset_id="document-dataset",
        name="Document tasks",
        skills=["document-method"],
        source={"kind": "document", "uris": ["viking://resources/source.md"]},
        cases=[{
            "case_id": "case-01",
            "query": "Verify the shipment using source.md and report the outcome.",
            "checks": [
                {"id": "R01", "kind": "output", "text": HIDDEN_REQUIREMENT},
                {"id": "R02", "kind": "trajectory", "text": "Read the supplied source document"},
            ],
            "materials": [{"path": "source.md", "content_b64": base64.b64encode(b"Shipment A: valid").decode()}],
            "replay": {
                "progressive_disclosure": {"batch_size": 1},
                "execution": document_replay_execution(),
            },
            "provenance": {
                "source_refs": ["viking://resources/source.md"],
                # These audit fields never become document execution inputs.
                "session_id": "not-an-execution-session",
                "source_runtime": {"type": "other-runtime"},
                "source_runtime_context": {"user_id": "untrusted-user"},
                "context_snapshot_id": "untrusted-snapshot",
                "context_snapshot": {"private": "not supplied"},
            },
        }],
    )["cases"][0]


def _job(current=None):
    return {
        "job_id": "document-job",
        "candidate_skill": _skill(),
        "current_skill": current,
        "replay_cases": [_case()],
        "source": {"kind": "skillminer"},
    }


@pytest.fixture
def restore_host():
    previous = current_host(required=False)
    yield
    configure_host(previous)


def test_document_binding_is_explicit_and_survives_common_spec():
    case = _case()
    assert document_runtime_type(case) == DOCUMENT_REPLAY_RUNTIME
    assert runtime_type_for_case(case) == DOCUMENT_REPLAY_RUNTIME
    spec = skill_replay_spec(_job())
    assert spec.cases[0]["replay"]["execution"] == document_replay_execution()
    assert spec.cases[0]["provenance"]["source_refs"] == ["viking://resources/source.md"]
    binding = document_replay_execution()
    binding["binding"] = "changed"
    assert document_replay_execution()["binding"] == "tenant_replay_adapter"


@pytest.mark.parametrize("execution", [
    None,
    {"kind": "document", "runtime_type": "other", "binding": "tenant_replay_adapter"},
    {"kind": "document", "runtime_type": "document", "binding": "unselected_factory"},
    {**document_replay_execution(), "endpoint": "http://untrusted.invalid"},
])
def test_document_binding_rejects_model_selected_execution(execution):
    case = _case()
    case["replay"]["execution"] = execution
    with pytest.raises(ReplayContractError):
        document_runtime_type(case)
    with pytest.raises(ReplayContractError):
        skill_replay_spec({**_job(), "replay_cases": [case]})


def test_runtime_matrix_preserves_document_execution_without_loading_session_context():
    case = _case()
    prepared = prepare_runtime_validation(
        skill={},
        sessions=[],
        replay_cases=[case],
        validation_runtimes=[DOCUMENT_REPLAY_RUNTIME],
    )
    assert prepared["policy"]["required_runtimes"] == [DOCUMENT_REPLAY_RUNTIME]
    assert prepared["policy"]["missing_replay_runtimes"] == []
    assert prepared["replay_cases"] == [case]


def test_runtime_matrix_neutral_runtime_does_not_inherit_document_marker():
    prepared = prepare_runtime_validation(
        skill={"portable": True},
        sessions=[],
        replay_cases=[_case()],
        validation_runtimes=[DOCUMENT_REPLAY_RUNTIME, "another-runtime"],
    )
    neutral = prepared["replay_cases"][1]
    assert "execution" not in neutral["replay"]
    assert runtime_type_for_case(neutral) == "another-runtime"


def test_execution_bundle_filters_only_assessment_assets_and_rehashes():
    treatment = _skill()
    original = copy.deepcopy(treatment)
    execution = document_execution_treatment(treatment)
    assert treatment == original
    assert set(decode_bundle_payload(execution["bundle"])) == {
        "SKILL.md", "references/rules.md", "scripts/verify.py",
    }
    assert execution["bundle"]["tree_sha256"] != treatment["bundle"]["tree_sha256"]
    assert validate_skill_treatment(execution)["passed"]
    peer_set = document_execution_treatment({"kind": "skill_set", "skills": [treatment, {**treatment, "name": "peer"}]})
    assert all("benchmark.json" not in decode_bundle_payload(member["bundle"]) for member in peer_set["skills"])


@pytest.mark.parametrize("update", [False, True])
def test_document_replay_calls_real_http_transport_with_isolated_branches(monkeypatch, restore_host, update):
    captured = []
    contexts = []
    sources = []
    sessions = []

    def transport(request):
        body = json.loads(request.content)
        captured.append(copy.deepcopy(body))
        candidate = body["branch"] == "candidate"
        answer = "complete with receipt" if candidate or body["turn_num"] == 2 else "needs receipt"
        return httpx.Response(200, json={
            "schema_version": REPLAY_TURN_RESULT_SCHEMA,
            "request_id": body["request_id"],
            "turn_num": body["turn_num"],
            "status": "succeeded",
            "final_response": answer,
            "messages": [
                {"role": "assistant", "tool_calls": [{"function": {
                    "name": "read_document", "arguments": '{"path":"source.md"}',
                }}]},
                {"role": "tool", "content": "Shipment A: valid"},
                {"role": "assistant", "content": answer},
            ],
            "metrics": {"tool_call_count": 1, "total_tokens": 50, "api_calls": 1},
            "trace_id": f"trace-{body['request_id']}-{body['turn_num']}",
        })

    client = httpx.Client(transport=httpx.MockTransport(transport))
    factory = TurnBasedReplayFactory(
        endpoint="http://agent.invalid/turn/document",
        runtime_type="customer-agent",
        isolated_sessions=True,
        post=client.post,
    )
    real_open = factory.open

    def open_session(context):
        contexts.append(copy.deepcopy(context))
        session = real_open(context)
        sessions.append(session)
        return session

    monkeypatch.setattr(factory, "open", open_session)

    class Host:
        def resolve_replay_factory(self, runtime_type, source):
            assert runtime_type == DOCUMENT_REPLAY_RUNTIME
            sources.append(copy.deepcopy(source))
            return factory

        def load_source_session(self, session_id):
            raise AssertionError("Document Replay must not load historical Sessions")

        def load_context_snapshot(self, snapshot_id, source):
            raise AssertionError("Document Replay must not load historical Context")

        def validate_skill_treatment(self, skill):
            return validate_skill_treatment(skill)

        def judge_harness(self):
            return {}

    configure_host(Host())

    def judge(**kwargs):
        complete = kwargs["interactions"][-1]["response"] == "complete with receipt"
        return {
            "judge": "model", "all_satisfied": complete,
            "items": [{**check, "satisfied": complete or check["kind"] == "trajectory", "evidence": "runtime trace"}
                      for check in kwargs["checklist"]],
        }

    def feedback(**kwargs):
        assert [item["text"] for item in kwargs["selected_items"]] == [HIDDEN_REQUIREMENT]
        return "Please include the verification receipt in the final report."

    monkeypatch.setattr(engine, "run_branch", lambda *a, **kw: run_branch(*a, **kw, judge=judge, feedback=feedback))
    data = _job(_skill("Published method") if update else None)
    original = copy.deepcopy(data)
    result = EmbeddedReplayGateway(engine.evaluate_job).evaluate(skill_replay_spec(data), max_interactions=3)
    client.close()

    assert result["status"] == "evaluated" and result["accepted"]
    assert result["runtime_type"] == DOCUMENT_REPLAY_RUNTIME
    assert data == original
    assert sources == [{"runtime": {"type": DOCUMENT_REPLAY_RUNTIME}, "runtime_context": {}}]
    assert len(contexts) == 2 and len(sessions) == 2
    assert contexts[0].request_id != contexts[1].request_id
    assert sessions[0] is not sessions[1] and all(session.closed for session in sessions)
    assert all(context.context_snapshot == {} for context in contexts)
    first = {body["branch"]: body for body in captured if body["turn_num"] == 1}
    assert first["baseline"]["materials"] == first["candidate"]["materials"] == data["replay_cases"][0]["materials"]
    assert first["baseline"]["context_snapshot"] == first["candidate"]["context_snapshot"] == {}
    assert first["candidate"]["skill"]["content"] == "Candidate method"
    if update:
        assert first["baseline"]["skill"]["content"] == "Published method"
    else:
        assert first["baseline"]["skill"] is None
    for body in first.values():
        assert HIDDEN_REQUIREMENT not in json.dumps(body)
        for member in body["skills"]:
            assert set(decode_bundle_payload(member["bundle"])) == {
                "SKILL.md", "references/rules.md", "scripts/verify.py",
            }
    later = next(body for body in captured if body["turn_num"] == 2)
    assert later["branch"] == "baseline" and "verification receipt" in later["prompt"]
    assert later["request_id"] == first["baseline"]["request_id"]
    assert later["history"] == [{"turn_num": 1, "prompt": first["baseline"]["prompt"], "response": "needs receipt"}]
    assert "skill" not in later and "materials" not in later
    branches = result["cases"][0]
    assert branches["baseline"]["interaction_turns"] == 2
    assert branches["candidate"]["interaction_turns"] == 1
    assert all(branch["session_id"] is None and branch["turn_num"] is None for branch in branches.values())
    assert all("read_document" in branch["trajectory"] for branch in branches.values())


def test_document_replay_without_factory_is_inconclusive(restore_host):
    configure_host(SimpleNamespace(
        validate_skill_treatment=validate_skill_treatment,
        resolve_replay_factory=lambda *_: None,
    ))
    result = engine.evaluate_job("document-job", job=_job())
    assert result["status"] == "skipped" and result["verdict"] == "inconclusive", result
    assert not result["accepted"] and not result["cases"]
    assert "Replay adapter bound" in result["reason"]
