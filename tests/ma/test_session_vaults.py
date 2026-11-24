import asyncio

import pytest

from team_ma.collection import runtime_snapshot


@pytest.mark.parametrize("vault_ids", [[], ["business-vault"], None, "invalid", "missing"])
def test_collection_records_native_session_vaults(monkeypatch, vault_ids):
    async def graph(*args, **kwargs):
        return {"entry_agent_id": "root", "agents": {"root": {"version": 1}}}
    monkeypatch.setattr("team_ma.collection.agent_graph", graph)
    session = {"agent": {"id": "root", "version": 1},
               "environment": {"id": "source", "config": {"type": "cloud"}}}
    if vault_ids != "missing":
        session["vault_ids"] = vault_ids
    snapshot = asyncio.run(runtime_snapshot(None, session))
    assert snapshot["snapshot_missing"] is (not isinstance(vault_ids, list) and vault_ids is not None)
    if vault_ids is None:
        assert snapshot["vault_ids"] == []
    if isinstance(vault_ids, list):
        assert snapshot["vault_ids"] == vault_ids
        assert snapshot["vault_ids"] is not vault_ids
