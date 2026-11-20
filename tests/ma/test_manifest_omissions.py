"""Exercise the real reconciliation gate with candidate and published bindings."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import team_ma.synchronization as synchronization


@pytest.mark.parametrize("placeholder_agents", [["root"], ["no-longer-a-member"]])
def test_unpublished_candidate_binding_does_not_block_sync(monkeypatch, placeholder_agents):
    run = asyncio.run(_reconcile(monkeypatch, placeholder_agents=placeholder_agents))
    assert run.repo.checkpoint.await_args.args[-1]["status"] == "applied"
    run.tombstones.assert_not_awaited()
    run.repo.bind.assert_awaited_once()
    assert run.repo.bind.await_args.kwargs["kind"] == "agent"


@pytest.mark.parametrize("binding_present", [False, True])
def test_missing_previously_applied_skill_still_requires_tombstone(monkeypatch, binding_present):
    with pytest.raises(ValueError, match="not a confirmed Skill deletion"):
        asyncio.run(_reconcile(monkeypatch, formal=True, binding_present=binding_present))


async def _reconcile(monkeypatch, *, placeholder_agents=None, formal=False, binding_present=True):
    @asynccontextmanager
    async def tenant_tx(*args):
        yield object()

    cloud = {"id": "env", "config": {"type": "cloud"}}
    graph = {"entry_agent_id": "root", "agents": {"root": {"id": "root", "version": "1", "skills": []}}}
    binding = ({"id": "skill", "version": "1", "agents": ["root"]} if formal
               else {"name": "candidate", "agents": placeholder_agents})
    previous = {"record": {"skills": {"missing": binding} if formal else {}, "environment": cloud}}
    repo = SimpleNamespace(
        applied=AsyncMock(return_value=previous),
        all_resources=AsyncMock(return_value=[{"logical_name": "missing", "record": binding}]
                               if binding_present else []),
        save_release=AsyncMock(), bind=AsyncMock(), checkpoint=AsyncMock(), operation_status=AsyncMock(),
    )
    tombstones = AsyncMock(return_value=[])
    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=tenant_tx),
        extras={"ma_skill_tombstones": tombstones},
        tasks=SimpleNamespace(require_current=AsyncMock()),
    )
    monkeypatch.setattr(synchronization, "MARepository", lambda tx: repo)
    monkeypatch.setattr(synchronization, "cleanup", AsyncMock())
    monkeypatch.setattr(synchronization, "pull", AsyncMock(return_value=({"skills": []}, "etag")))
    monkeypatch.setattr(synchronization, "Effects", lambda *a, **kw: None)
    monkeypatch.setattr(synchronization, "agent_graph", AsyncMock(return_value=graph))
    client = SimpleNamespace(request=AsyncMock(return_value=cloud))
    task = SimpleNamespace(tenant_id="alpha", subject_id="one", task_id="sync")
    connection = {"config": {"entry_agent_id": "root"}, "revision": 1, "config_hash": "hash"}
    await synchronization.reconcile_connection(task, services, client, connection, {"checkpoint": {}})
    return SimpleNamespace(repo=repo, tombstones=tombstones)
