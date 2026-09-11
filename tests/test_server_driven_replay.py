from __future__ import annotations

import pytest

from teamEvolver import true_replay as tr
from teamEvolver.integrations.agent_protocol import (
    REPLAY_TURN_RESULT_SCHEMA_V1,
)


class _FakeAdapter:
    """Records turn requests, replies with a scripted success sequence."""

    instances: list["_FakeAdapter"] = []

    def __init__(self, *, endpoint, runtime_type, auth_profile="", api_key=""):
        self.endpoint = endpoint
        self.runtime_type = runtime_type
        self.auth_profile = auth_profile
        self.calls: list[dict] = []
        _FakeAdapter.instances.append(self)

    def call_turn(self, request: dict) -> dict:
        self.calls.append(request)
        turn_num = request["turn_num"]
        return {
            "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
            "protocol_version": "1.0",
            "request_id": request["request_id"],
            "turn_num": turn_num,
            "runtime": {"type": self.runtime_type},
            "status": "succeeded",
            "final_response": f"resp-{turn_num}",
            "messages": [{"role": "assistant", "content": f"step-{turn_num}"}],
            "metrics": {"tool_call_count": 1, "total_tokens": 10},
            "artifacts": [{"path": "out.txt", "size": 4}] if turn_num == 2 else [],
        }


@pytest.fixture()
def server_driven_env(monkeypatch):
    monkeypatch.setattr(tr, "TurnBasedReplayAdapter", _FakeAdapter)
    monkeypatch.setattr(
        tr,
        "read_team_evolver_harness",
        lambda: {"base_url": "http://judge", "api_key": "k", "model": "m"},
    )
    monkeypatch.setattr(
        tr,
        "_checklist_judge_config",
        lambda: {"system_prompt": "s", "temperature": 0},
    )
    _FakeAdapter.instances.clear()
    return _FakeAdapter


def _run_branch(**overrides) -> dict:
    case_overrides = {
        key: value
        for key, value in overrides.items()
        if key in {"checklist", "progressive_disclosure", "materials", "execution_manifest"}
    }
    capability_overrides = overrides.get("capability_overrides") or {}
    case = {
        "index": 0,
        "checklist": [{"id": "C1", "text": "must do X"}],
        "progressive_disclosure": {"batch_size": 2},
        "materials": [{"path": "src/input.txt"}],
        "execution_manifest": {"model": "m"},
    }
    case.update(case_overrides)
    capability = {
        "orchestration": "server_driven",
        "max_interactions": 4,
        "endpoint": "https://agent.example/turn",
        "auth_profile": "demo",
    }
    capability.update(capability_overrides)
    return tr._spawn_server_driven_branch(
        branch="candidate",
        instruction="do the task",
        branch_skill={"name": "demo", "content": "# demo"},
        job={"job_id": "job-1", "candidate_skill": {"name": "demo"}},
        case=case,
        source_session={"runtime": {"type": "demo"}},
        timeout=120,
        max_interactions=4,
        runtime_type="demo",
        capability=capability,
        endpoint=capability["endpoint"],
    )


def test_server_driven_branch_discloses_and_aggregates(server_driven_env, monkeypatch):
    reports = iter(
        [
            {
                "items": [{"id": "C1", "satisfied": False, "evidence": "not yet"}],
                "all_satisfied": False,
            },
            {
                "items": [{"id": "C1", "satisfied": True, "evidence": "done"}],
                "all_satisfied": True,
            },
        ]
    )
    judge_calls: list[dict] = []

    def fake_judge(**kwargs):
        judge_calls.append(kwargs)
        return next(reports)

    monkeypatch.setattr(tr, "_evaluate_local_checklist", fake_judge)

    result = _run_branch()
    adapter = _FakeAdapter.instances[-1]

    assert len(adapter.calls) == 2
    # Same session handle across turns; skill/materials only on turn 1.
    assert adapter.calls[0]["request_id"] == adapter.calls[1]["request_id"]
    assert adapter.calls[0]["prompt"] == "do the task"
    assert adapter.calls[0]["skill"] == {"name": "demo", "content": "# demo"}
    assert adapter.calls[0]["materials"] == [{"path": "src/input.txt"}]
    assert "skill" not in adapter.calls[1]
    assert "context_snapshot" not in adapter.calls[1]
    # Progressive disclosure: turn 2 carries the judge-generated follow-up.
    assert "[C1]" in adapter.calls[1]["prompt"]
    assert "must do X" in adapter.calls[1]["prompt"]
    # Judge never sees an empty evidence set once artifacts arrive.
    assert judge_calls[1]["artifacts"] == [{"path": "out.txt", "size": 4}]
    # Server-side aggregation.
    assert result["ok"] is True
    assert result["interaction_turns"] == 2
    assert result["tool_call_count"] == 2
    assert result["total_tokens"] == 20
    assert result["checklist_report"]["all_satisfied"] is True
    assert result["checklist_report"]["rounds"] == 2
    assert result["context_input_hash"].startswith("sha256:")
    assert result["request_id"] == adapter.calls[0]["request_id"]


def test_server_driven_branch_never_sends_checklist(server_driven_env, monkeypatch):
    monkeypatch.setattr(
        tr,
        "_evaluate_local_checklist",
        lambda **_kwargs: {
            "items": [{"id": "C1", "satisfied": True, "evidence": "ok"}],
            "all_satisfied": True,
        },
    )

    result = _run_branch()
    adapter = _FakeAdapter.instances[-1]

    assert result["ok"] is True
    for request in adapter.calls:
        assert "checklist" not in request
        assert "progressive_disclosure" not in request


def test_server_driven_branch_plain_http_endpoint(server_driven_env, monkeypatch):
    """Zero agent-side awareness: MappedHttpAdapter drives a plain endpoint."""
    from teamEvolver.integrations.replay_adapters import MappedHttpAdapter

    captured_bodies: list[dict] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "answer": f"resp-{len(captured_bodies)}",
                "messages": [{"role": "assistant", "content": "step"}],
                "usage": {"tool_calls": 2, "total_tokens": 50},
            }

    def fake_post(_url, *, json=None, **_kwargs):
        captured_bodies.append(json)
        return _Resp()

    monkeypatch.setattr(
        tr,
        "MappedHttpAdapter",
        lambda **kwargs: MappedHttpAdapter(post=fake_post, **kwargs),
    )
    monkeypatch.setattr(
        tr,
        "_evaluate_local_checklist",
        lambda **_kwargs: {
            "items": [{"id": "C1", "satisfied": True, "evidence": "ok"}],
            "all_satisfied": True,
        },
    )

    result = _run_branch(
        capability_overrides={
            "request_template": {
                "message": "{{prompt}}",
                "session_id": "{{request_id}}",
                "history": "{{history}}",
                "skill": "{{skill_content}}",
            },
            "response_mapping": {
                "final_response": "answer",
                "messages": "messages",
                "tool_call_count": "usage.tool_calls",
                "total_tokens": "usage.total_tokens",
            },
        }
    )

    assert result["ok"] is True
    assert result["interaction_turns"] == 1
    assert result["tool_call_count"] == 2
    assert result["total_tokens"] == 50
    assert result["metrics_incomplete"] is False
    # The agent received its own request shape, never the teamEvolver protocol.
    body = captured_bodies[0]
    assert body["message"] == "do the task"
    assert body["skill"] == "# demo"
    assert body["history"] == []
    assert "checklist" not in body and "schema_version" not in body


def test_server_driven_branch_fails_closed_on_unsupported(
    server_driven_env, monkeypatch
):
    def unsupported(self, request):
        return {
            "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
            "protocol_version": "1.0",
            "request_id": request["request_id"],
            "turn_num": request["turn_num"],
            "status": "unsupported",
            "error": {
                "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                "message": "external tool call cannot be deterministically replayed",
                "retryable": False,
            },
        }

    monkeypatch.setattr(_FakeAdapter, "call_turn", unsupported)
    monkeypatch.setattr(
        tr,
        "_evaluate_local_checklist",
        lambda **_kwargs: {"items": [], "all_satisfied": True},
    )

    result = _run_branch()

    assert result["ok"] is False
    assert result["error_code"] == "REPLAY_EXTERNAL_TOOL_UNSUPPORTED"


def test_server_driven_branch_fails_closed_on_missing_turn_metrics(monkeypatch):
    from teamEvolver.integrations.replay_adapters import TurnBasedReplayAdapter

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return captured["payload"]

    def post(_url, *, json=None, **_kwargs):
        captured["payload"] = {
            "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
            "protocol_version": "1.0",
            "request_id": json["request_id"],
            "turn_num": json["turn_num"],
            "status": "succeeded",
            "metrics": {"tool_call_count": 1},
        }
        return _Resp()

    monkeypatch.setattr(
        tr,
        "TurnBasedReplayAdapter",
        lambda **kwargs: TurnBasedReplayAdapter(post=post, **kwargs),
    )
    monkeypatch.setattr(
        tr,
        "read_team_evolver_harness",
        lambda: {"base_url": "http://judge", "api_key": "k", "model": "m"},
    )
    monkeypatch.setattr(
        tr,
        "_checklist_judge_config",
        lambda: {"system_prompt": "s", "temperature": 0},
    )
    monkeypatch.setattr(
        tr,
        "_evaluate_local_checklist",
        lambda **_kwargs: {"items": [], "all_satisfied": True},
    )

    result = _run_branch()

    # Fail-closed: missing total_tokens must invalidate the turn.
    assert result["ok"] is False
    assert "total_tokens" in result["error"]
