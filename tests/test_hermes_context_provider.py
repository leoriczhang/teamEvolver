from __future__ import annotations

import sys
from types import ModuleType

import pytest


def _provider_class(monkeypatch):
    memory_provider = ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    memory_provider.MemoryProvider = MemoryProvider
    agent = ModuleType("agent")
    agent.memory_provider = memory_provider
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    monkeypatch.delitem(
        sys.modules,
        "teamEvolver.integrations.hermes_context_provider",
        raising=False,
    )
    from teamEvolver.integrations.hermes_context_provider import (
        TeamEvolverMemoryProvider,
    )

    return TeamEvolverMemoryProvider


def test_provider_commits_only_successfully_expanded_context_refs(
    monkeypatch,
) -> None:
    provider = _provider_class(monkeypatch)()
    provider._context_session_id = "ctxs-demo"
    calls: list[tuple[str, str, dict]] = []

    def request(method, path, *, body=None, **_kwargs):
        calls.append((method, path, dict(body or {})))
        if path.endswith("/read") and body["context_ref"] == "ctx-failed":
            raise RuntimeError("read failed")
        return {"content": "expanded"}

    provider._request = request
    with pytest.raises(RuntimeError, match="read failed"):
        provider.handle_tool_call(
            "team_evolver_context_read",
            {"context_ref": "ctx-failed"},
        )
    provider.handle_tool_call(
        "team_evolver_context_read",
        {"context_ref": "ctx-b"},
    )
    provider.handle_tool_call(
        "team_evolver_context_read",
        {"context_ref": "ctx-a"},
    )
    provider.handle_tool_call(
        "team_evolver_context_read",
        {"context_ref": "ctx-a"},
    )

    provider.on_session_end([])

    commit = next(call for call in calls if call[1].endswith("/commit"))
    assert commit[2] == {
        "context_session_id": "ctxs-demo",
        "used_context_refs": ["ctx-a", "ctx-b"],
    }
    assert provider._used_context_refs == set()


def test_provider_retains_usage_refs_when_commit_fails(monkeypatch) -> None:
    provider = _provider_class(monkeypatch)()
    provider._context_session_id = "ctxs-demo"
    provider._used_context_refs.add("ctx-a")
    provider._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("commit failed")
    )

    provider.on_session_end([])

    assert provider._used_context_refs == {"ctx-a"}
