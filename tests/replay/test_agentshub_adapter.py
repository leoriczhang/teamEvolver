"""Exercise uploaded AgentsHub source through the production loader."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from team_replay.adapters.loader import build_from_content
from team_replay.engine import make_context
from team_replay.hooks import ReplayContext, ReplayTreatment, ReplayUnsupported

SOURCE = Path(__file__).parents[2] / "team_replay/adapters/examples/agentshub.py"


def factory():
    return build_from_content(
        SOURCE.read_text(), "agentshub.py",
        SimpleNamespace(agentshub={
            "backend_root": "/opt/agentshub/backend",
            "tenant_id": "default", "agent_id": "agent", "user_id": "admin",
        }),
        tenant_id="default",
    )


def context(branch="candidate", skill=None):
    return ReplayContext(
        request_id="request-" + branch, runtime_type="agentshub",
        treatment=ReplayTreatment(branch, skill), materials=(), context_snapshot={}, timeout_seconds=30,
    )


def test_uploaded_factory_accepts_runtime_configuration():
    bound = factory()
    a = bound.open(context("baseline"))
    b = bound.open(context("candidate"))
    assert a is not b
    a.close()
    b.close()


def test_full_bundle_survives_adapter_and_absent_baseline_stays_absent():
    skill = {
        "name": "example", "description": "A skill", "content": "Instructions",
        "bundle": {"format": "skill-bundle", "files": [
            {"path": "SKILL.md", "encoding": "utf-8", "content": "Instructions"},
            {"path": "scripts/run.py", "encoding": "utf-8", "content": "print(42)"},
        ]},
    }
    session = factory().open(context(skill=skill))
    assert session.startup["skill"] == skill
    assert factory().open(context("baseline")).startup["skill"] is None


def test_remote_runtime_is_not_silently_executed_locally():
    bound = factory()
    ctx = context()
    ctx = ReplayContext(
        request_id=ctx.request_id, runtime_type="other", treatment=ctx.treatment,
        materials=(), context_snapshot={}, timeout_seconds=30,
    )
    with pytest.raises(ReplayUnsupported):
        bound.open(ctx)


def test_recorded_source_identity_and_turn_are_fixed_for_adapter():
    source = {"runtime": {"type": "agentshub"}, "runtime_context": {"agent_id": "original"}}
    ctx = make_context("baseline", None, {"provenance": {"turn_num": 3}}, source, 30, snapshot={})
    source["runtime_context"]["agent_id"] = "changed"
    session = factory().open(ctx)
    assert session.startup["source_session"]["runtime_context"]["agent_id"] == "original"
    assert session.startup["source_turn_num"] == 3


def runtime_functions():
    bound = factory()
    namespace = bound.factory.open.__globals__
    runtime = {"__name__": "test_runtime"}
    exec(compile(namespace["AGENTSHUB_RUNTIME"], "agentshub-runtime.py", "exec"), runtime)
    return runtime


def test_missing_usage_is_unavailable_instead_of_a_zero_bill():
    usage = runtime_functions()["observed_usage"]
    assert "total_tokens" not in usage([])
    assert "total_tokens" not in usage([SimpleNamespace(event_type="llm_call_finished", payload_json={})])
    events = [SimpleNamespace(event_type="llm_call_finished", payload_json={
        "usage": {"input_tokens": 13, "output_tokens": 7},
    })]
    assert usage(events)["total_tokens"] == 20
    events.append(SimpleNamespace(event_type="llm_call_finished", payload_json={}))
    assert "total_tokens" not in usage(events)


def test_send_reuses_process_and_failed_execution_cannot_be_judged_as_a_reply(monkeypatch):
    session = factory().open(context())
    starts, prompts = [], []

    class Connection:
        def settimeout(self, timeout):
            pass

        def sendall(self, message):
            prompts.append(message)

    def start():
        starts.append(True)
        session.process = object()
        session.connection = Connection()
        session.deadline = __import__("time").monotonic() + 30

    monkeypatch.setattr(session, "_start", start)
    observations = iter([
        {"status": "succeeded", "turn_num": 1, "response": "one", "metrics": {"total_tokens": 9}},
        {"status": "succeeded", "turn_num": 2, "response": "two", "metrics": {"total_tokens": 11}},
        {"status": "failed", "turn_num": 3, "error": "model failed"},
    ])
    monkeypatch.setattr(session, "_receive", lambda: next(observations))
    assert session.send("first").metrics["total_tokens"] == 9
    assert session.send("second").metrics["total_tokens"] == 11
    assert len(starts) == 1
    with pytest.raises(RuntimeError, match="model failed"):
        session.send("third")
    assert len(prompts) == 3
