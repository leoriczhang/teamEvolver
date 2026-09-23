"""Factory execution boundaries, disclosure gate and runtime-only metrics."""

import json
from dataclasses import fields
from types import SimpleNamespace

import pytest

from team_replay.adapter_runtime import AdapterConflict, AdapterError, available, binding, load_factory, save_content
from team_replay.artifacts import validate_skill_treatment
from team_replay.execution import run_branch
from team_replay.factories import DeapReplayFactory, LegacyBranchSession, MappedHttpReplayFactory, TurnBasedReplayFactory
from team_replay.hooks import AgentObservation, ReplayContext, ReplayTreatment, ReplayUnsupported
from team_replay.judging import judge_checklist, render_user_feedback
from team_replay.local_hermes import build_sandbox
from team_replay.metrics import compare_efficiency
from team_replay.policy import progressive_replay_decision


def context(branch="baseline"):
    return ReplayContext(
        f"request-{branch}", "customer", ReplayTreatment(branch, None), (), {}, 60,
    )


def test_runtime_binding_is_explicit_and_read_save_do_not_execute(tmp_path):
    config = SimpleNamespace(replay_adapters_dir=str(tmp_path), replay_adapter="customer.py")
    tenant = SimpleNamespace(tenant_id="tenant-a", config_overrides={})
    assert binding(config, tenant) == ""
    with pytest.raises(AdapterError, match="No Replay adapter"):
        load_factory(config, tenant)
    code = '''
REPLAY_ADAPTER = {"label": "Customer", "enabled": True}
class Factory:
    def open(self, context):
        raise RuntimeError("only a test factory")
def build_replay_adapter(config):
    return Factory()
'''
    saved = save_content(config, "customer.py", code)
    assert available(config)[0]["label"] == "Customer"
    tenant.config_overrides["replay_adapter"] = "customer.py"
    assert callable(load_factory(config, tenant, saved["revision"]).open)
    with pytest.raises(AdapterConflict):
        save_content(config, "customer.py", code)
    with pytest.raises(AdapterConflict):
        load_factory(config, tenant, "old revision")
    with pytest.raises(AdapterError):
        save_content(config, "../escape.py", code)
    trap = code + '\nraise RuntimeError("do not execute when listing/saving")\n'
    save_content(config, "customer.py", trap, saved["revision"])
    assert available(config)[0]["enabled"] is True


def test_progressive_loop_only_sends_query_then_selected_natural_feedback():
    sent, received_contexts, closed = [], [], []
    checklist = [
        {"id": "R01", "text": "deliver the file"},
        {"id": "R02", "text": "include costs"},
        {"id": "R03", "text": "hidden audit appendix"},
    ]

    class Session:
        def send(self, message):
            sent.append(message)
            return AgentObservation("Produced file" if len(sent) == 1 else "Complete",
                                    metrics={"total_tokens": 10} if len(sent) == 1 else {})

        def close(self):
            closed.append(True)

    class Factory:
        def open(self, value):
            received_contexts.append(value)
            assert {field.name for field in fields(value)} == {
                "request_id", "runtime_type", "treatment", "materials", "context_snapshot", "timeout_seconds",
            }
            return Session()

    def judge(**kwargs):
        def complete(**args):
            satisfied = len(sent) > 1
            return {
                "items": [{"id": item["id"], "satisfied": satisfied or item["id"] == "R01",
                           "evidence": "file output" if satisfied or item["id"] == "R01" else ""}
                          for item in args["payload"]["checklist"]],
                "all_satisfied": satisfied, "positive_observations": ["file was produced"],
            }
        return judge_checklist(**kwargs, completion=complete)

    def feedback(**kwargs):
        assert [item["id"] for item in kwargs["selected_items"]] == ["R02"]
        assert "hidden audit" not in json.dumps(kwargs)
        return render_user_feedback(**kwargs, completion=lambda **_: {"message": "The file is ready; please add the costs."})

    result = run_branch(
        Factory(), context(), {"query": "Make the deliverable", "checklist": checklist,
                              "progressive_disclosure": {"batch_size": 1}},
        harness={}, max_interactions=4, judge=judge, feedback=feedback,
    )
    assert sent == ["Make the deliverable", "The file is ready; please add the costs."]
    assert closed == [True] and len(received_contexts) == 1
    assert result["completed"] is True and result["interaction_turns"] == 2
    assert result["total_tokens"] == "unavailable" and result["tool_call_count"] == "unavailable"
    assert result["disclosures"][0]["ids"] == ["R02"]


def test_judge_requires_evidence_and_invalid_output_fails_closed():
    kwargs = dict(
        harness={}, checklist=[{"id": "R01", "text": "file"}], interactions=[], messages=[], artifacts=[],
    )
    result = judge_checklist(**kwargs, completion=lambda **_: {
        "items": [{"id": "R01", "satisfied": True, "evidence": ""}],
        "all_satisfied": True, "positive_observations": ["unsupported praise"],
    })
    assert not result["all_satisfied"] and not result["positive_observations"]
    failed = judge_checklist(**kwargs, completion=lambda **_: {"items": []})
    assert failed["judge"] == "unavailable" and not failed["all_satisfied"]


@pytest.mark.parametrize("message", ["Checklist R01 is incomplete", "Candidate needs changes", "第 2 轮请补充", "Add R01"])
def test_simulator_cannot_emit_evaluation_terms(message):
    with pytest.raises(ValueError):
        render_user_feedback(
            harness={}, response="response", positive_observations=[], selected_items=[{"id": "R01", "text": "costs"}],
            disclosed_requirements=[], round_number=2, completion=lambda **_: {"message": message},
        )


def test_objective_decisions_require_completion_and_never_zero_fill():
    efficiency = compare_efficiency({"interaction_turns": 3}, {"interaction_turns": 2})
    assert efficiency["dimensions"]["total_tokens"]["winner"] == "unavailable"
    incomplete = {"total": 1, "all_satisfied": False}
    passed = {"total": 1, "all_satisfied": True}
    assert progressive_replay_decision(efficiency=efficiency, baseline_checklist=incomplete,
                                       candidate_checklist=incomplete)["verdict"] == "inconclusive"
    assert progressive_replay_decision(efficiency=efficiency, baseline_checklist=passed,
                                       candidate_checklist=incomplete)["verdict"] == "reject"
    assert progressive_replay_decision(efficiency=efficiency, baseline_checklist=incomplete,
                                       candidate_checklist=passed)["verdict"] == "accept"
    assert progressive_replay_decision(efficiency=efficiency, baseline_checklist=passed,
                                       candidate_checklist=passed)["verdict"] == "accept"
    assert progressive_replay_decision(efficiency=efficiency, baseline_checklist={**incomplete, "judge": "unavailable"},
                                       candidate_checklist=passed)["verdict"] == "inconclusive"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_turn_and_mapped_factories_hold_independent_history_and_cleanup():
    calls = []

    def post(endpoint, *, json, **kwargs):
        calls.append(json)
        return Response({
            "schema_version": "teamevolver.replay-turn-result.v1", "protocol_version": "1.0",
            "status": "succeeded", "request_id": json["request_id"], "turn_num": json["turn_num"],
            "final_response": "ok", "metrics": {}, "answer": "mapped reply",
        })

    factory = TurnBasedReplayFactory("http://example.invalid", "customer", isolated_sessions=True, post=post)
    a, b = factory.open(context()), factory.open(context("candidate"))
    assert a is not b
    a.send("query")
    a.send("feedback")
    b.send("query")
    assert calls[1]["history"] == [{"turn_num": 1, "prompt": "query", "response": "ok"}]
    assert calls[2]["history"] == []
    a.close()
    a.close()
    with pytest.raises(RuntimeError, match="closed"):
        a.send("again")
    mapped = MappedHttpReplayFactory(
        "http://example.invalid", "customer", isolated_sessions=True, post=post,
        request_template={"request_id": "{{request_id}}", "turn_num": "{{turn_num}}", "prompt": "{{prompt}}"},
        response_mapping={"final_response": "answer"},
    ).open(context())
    assert mapped.send("hello").metrics == {}
    mapped.close()


def test_turn_factory_forwards_peer_skill_set():
    calls = []

    def post(endpoint, *, json, **kwargs):
        calls.append(json)
        return Response({
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "status": "succeeded",
            "request_id": json["request_id"],
            "turn_num": json["turn_num"],
            "final_response": "ok",
            "metrics": {},
        })

    treatment = {
        "kind": "skill_set",
        "skills": [
            {"name": "skill-a", "description": "A", "content": "Do A"},
            {"name": "skill-b", "description": "B", "content": "Do B"},
        ],
    }
    replay_context = ReplayContext(
        "request-peer",
        "customer",
        ReplayTreatment("candidate", treatment),
        (),
        {},
        60,
    )
    session = TurnBasedReplayFactory(
        "http://example.invalid",
        "customer",
        isolated_sessions=True,
        post=post,
    ).open(replay_context)

    session.send("query")

    assert validate_skill_treatment(treatment)["passed"] is True
    assert [item["name"] for item in calls[0]["skills"]] == [
        "skill-a",
        "skill-b",
    ]


def test_local_sandbox_installs_all_peer_skills(tmp_path):
    sandbox = build_sandbox(
        tmp_path,
        "candidate",
        {
            "base_url": "http://example.invalid",
            "api_key": "",
            "model": "test",
            "max_tokens": 100,
            "api_mode": "",
        },
        {
            "kind": "skill_set",
            "skills": [
                {"name": "skill-a", "description": "A", "content": "Do A"},
                {"name": "skill-b", "description": "B", "content": "Do B"},
            ],
        },
    )

    home = tmp_path / "candidate" / ".hermes" / "skills"
    assert (home / "skill-a" / "SKILL.md").is_file()
    assert (home / "skill-b" / "SKILL.md").is_file()
    assert sandbox["skill_tree_sha256"]


def test_deap_factory_creates_independent_workspaces_and_rejects_unsupported_materials():
    clients = []

    class Client:
        def __init__(self):
            self.calls = []
            clients.append(self)

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs["json"]))
            return Response({"answer": "DEAP reply", "success": True})

    factory = DeapReplayFactory("http://example.invalid", client_factory=Client)
    a, b = factory.open(context()), factory.open(context("candidate"))
    assert a.adapter.workspace != b.adapter.workspace
    assert a.send("query").metrics == {}
    a.close()
    assert clients[0].calls[-1][0].endswith("/skillopt/delete")
    c = ReplayContext("req", "deap", ReplayTreatment("baseline", None), ({"content": "material"},), {}, 60)
    with pytest.raises(ReplayUnsupported):
        factory.open(c)


def test_deap_factory_maps_replay_trace_and_metrics():
    class Client:
        def post(self, url, **_kwargs):
            if url.endswith("/skillopt/delete"):
                return Response({"success": True})
            return Response(
                {
                    "answer": "DEAP reply",
                    "success": True,
                    "traceId": "trace-1",
                    "replay": {
                        "messages": [
                            {
                                "role": "assistant",
                                "content": "DEAP reply",
                                "tool_calls": [{"name": "read"}],
                            }
                        ],
                        "metrics": {
                            "tool_call_count": 1,
                            "total_tokens": 42,
                            "input_tokens": 30,
                            "output_tokens": 12,
                        },
                        "artifacts": [{"path": "result.txt"}],
                    },
                }
            )

    session = DeapReplayFactory(
        "http://example.invalid",
        client_factory=Client,
    ).open(context())

    observation = session.send("query")
    session.close()

    assert observation.trace_id == "trace-1"
    assert observation.metrics == {
        "tool_call_count": 1,
        "total_tokens": 42,
        "input_tokens": 30,
        "output_tokens": 12,
    }
    assert observation.metrics_incomplete_reason == ""
    assert observation.messages[0]["tool_calls"][0]["name"] == "read"


def test_legacy_bridge_is_single_turn_and_hides_checklist():
    calls = []

    class Adapter:
        def execute_branch(self, request):
            calls.append(request)
            return {"status": "succeeded", "output": {"final_response": "answer"},
                    "trace": {}, "metrics": {"interaction_turns": 1}}

    session = LegacyBranchSession(context(), Adapter())
    assert session.send("query").response == "answer"
    assert calls[0]["case"] == {"query": "query", "materials": []}
    with pytest.raises(ReplayUnsupported):
        session.send("follow-up")
    session.close()


@pytest.mark.parametrize("path", ["/tmp/escape", "../escape", "C:/escape", "a/../../escape"])
def test_material_paths_cannot_escape_workspace(path):
    from team_replay._util import normalize_artifact_rel_path
    with pytest.raises(ValueError):
        normalize_artifact_rel_path(path)
