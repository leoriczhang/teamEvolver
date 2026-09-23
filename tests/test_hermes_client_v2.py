"""Hermes client protocol, installation and durable local ownership."""

import importlib
import json
import sys
from types import ModuleType

import pytest

from session_ingestion.push.hermes import install, push_session
from teamEvolver.integrations.hermes_delivery import HermesDeliverySpool, producer_id


def test_installer_writes_only_tenant_user_config_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("installer must not register"))
    assert install.main([
        "--user-id", "alice", "--url", "https://example.invalid",
        "--tenant-token", "tevt_a", "--hermes-home", str(tmp_path), "--no-hook",
    ]) == 0
    config = tmp_path / "skills" / "teamEvolver-feed" / "feed.json"
    assert json.loads(config.read_text()) == {
        "base_url": "https://example.invalid", "tenant_token": "tevt_a", "user_id": "alice",
    }
    assert config.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "teamEvolver-feed-spool").exists()


def test_session_sender_uses_v2_and_tenant_token(tmp_path, monkeypatch):
    cfg = {"base_url": "https://example.invalid", "tenant_token": "tevt_a", "user_id": "alice", "spool_dir": str(tmp_path)}
    monkeypatch.setattr(push_session, "_context_usage_records", lambda _: [])
    session = push_session._v2_session({"session_id": "session", "turns": [{"prompt_text": "hello"}]}, cfg, "alice")
    assert session["schema_version"] == "teamevolver.agent-session.v2"
    assert session["runtime_context"]["user_id"] == "alice"
    assert "integration_id" not in session["runtime"]
    captured = []
    monkeypatch.setattr(push_session, "_post", lambda *args: captured.append(args) or True)
    spool = push_session._delivery_spool(cfg)
    delivery = push_session._spool(session, cfg)
    result = spool.deliver(delivery["delivery_id"], push_session._delivery_sender(cfg, base_url=cfg["base_url"], user="alice"))
    assert result["status"] == "acked"
    assert captured[0][-1] == "tevt_a"


def test_spool_partitions_subject_and_destination(tmp_path):
    a = producer_id({"base_url": "url", "user_id": "alice", "tenant_token": "tevt_a"})
    b = producer_id({"base_url": "url", "user_id": "alice", "tenant_token": "tevt_b"})
    assert a != b
    spool_a = HermesDeliverySpool(tmp_path, producer_id=a)
    spool_b = HermesDeliverySpool(tmp_path, producer_id=b)
    old = spool_a.enqueue(kind="context.start", aggregate_id="same", sequence=1, payload={"user_id": "alice"})
    assert spool_a.enqueue(kind="context.start", aggregate_id="same", sequence=1, payload={"user_id": "alice"}) == old
    assert spool_b.health()["backlog"] == 0
    with pytest.raises(ValueError, match="another producer"):
        spool_b.deliver(old["delivery_id"], lambda _: {})
    assert spool_a.deliver(old["delivery_id"], lambda _: {"ok": True})["status"] == "acked"


def test_context_provider_sends_user_id_on_all_operations(tmp_path, monkeypatch):
    stub = ModuleType("agent.memory_provider")
    stub.MemoryProvider = object
    monkeypatch.setitem(sys.modules, "agent.memory_provider", stub)
    provider_module = importlib.import_module("teamEvolver.integrations.hermes_context_provider")
    cfg = {"base_url": "url", "user_id": "alice", "tenant_token": "tevt_a", "spool_dir": str(tmp_path)}
    monkeypatch.setattr(provider_module, "_load_feed", lambda: cfg)
    provider = provider_module.TeamEvolverMemoryProvider()
    calls = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs["body"]))
        return {"context_session_id": "context-session", "items": []}

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(provider, "_record_usage", lambda *a: None)
    assert provider.is_available()
    provider.initialize("session")
    provider.prefetch("query")
    provider.handle_tool_call("team_evolver_context_read", {"context_ref": "ref"})
    provider.handle_tool_call("team_evolver_memory_remember", {"content": "memory"})
    provider.handle_tool_call("team_evolver_memory_forget", {"context_ref": "ref"})
    provider.sync_turn("hello", "response")
    provider.on_session_end([])
    assert len(calls) == 8
    assert all(body["user_id"] == "alice" and "integration_id" not in body for _, body in calls)
