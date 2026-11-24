import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from team_ma.queries import freeze


@pytest.mark.parametrize("vault_ids", [[], ["session-vault"], None])
def test_replay_freezes_session_references_despite_connection_drift(monkeypatch, vault_ids):
    graph = {"entry_agent_id": "root", "agents": {"root": {"version": 1}}}
    environment = {"id": "historical", "config": {"type": "cloud", "setup_script": "recorded"}}
    revisions = []
    class Repo:
        async def connection(self, conn, ident, revision=None):
            revisions.append(revision)
            return {"revision": revision or 3, "enabled": True, "config_hash": "current",
                    "config": {"replay_enabled": True, "vault_ids": ["current-or-recorded-vault"],
                               "runtime_binding": {"ready_probe_message": "read ready"}}}
        async def all_resources(self, conn, ident, kind):
            return [{"logical_name": "note", "record": {"agents": ["root"]}}]
    @asynccontextmanager
    async def tenant_tx(tenant):
        yield None
    monkeypatch.setattr("team_ma.queries.MARepository", lambda tx: Repo())
    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx))
    cases = [{"case_id": "case", "provenance": {"source_runtime": {
        "type": "managed-agents", "connection_id": "one", "connection_revision": 1,
        "agent_graph": graph, "environment": environment, "vault_ids": vault_ids,
    }}}]
    if vault_ids is None:
        with pytest.raises(ValueError, match="recollect the source Session"):
            asyncio.run(freeze(services, "tenant", cases, "note"))
        return
    value = asyncio.run(freeze(services, "tenant", cases, "note"))["case"]
    assert revisions == [1, None]
    assert value["connection_revision"] == 3
    assert value["agent_graph"] == graph and value["environment"] == environment
    assert value["replay_environment"] == environment
    assert value["config"]["replay_environment_id"] == "historical"
    assert value["config"]["replay_vault_ids"] == vault_ids
