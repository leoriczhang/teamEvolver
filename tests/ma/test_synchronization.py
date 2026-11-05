import asyncio
import copy

import httpx
import pytest

from team_ma.synchronization import update_agent
from tests.support.managed_agents import FakeEffects


def test_binding_changes_remove_old_member_add_new_member_and_preserve_unmanaged():
    agents = {
        "old": {"id": "old", "version": 1, "system": "preserve", "skills": [
            {"type": "custom", "skill_id": "note", "version": "1"}, {"type": "ark", "skill_id": "preset"}]},
        "new": {"id": "new", "version": 4, "system": "new member", "skills": []},
    }
    class Client:
        async def request(self, method, path, **kwargs):
            ident = path.split("/")[1]
            if method == "POST":
                agents[ident] = {"id": ident, **kwargs["json"], "version": agents[ident]["version"] + 1}
            return copy.deepcopy(agents[ident])
    previous = {"note-craft": {"id": "note", "version": "1", "agents": ["old"]}}
    desired = {"note-craft": {"id": "note", "version": "1", "agents": ["new"]}}
    async def run():
        for ident in agents:
            await update_agent(Client(), FakeEffects(), ident, desired, desired, {}, set(), "release", previous)
    asyncio.run(run())
    assert agents["old"]["skills"] == [{"type": "ark", "skill_id": "preset"}]
    assert agents["old"]["system"] == "preserve"
    assert agents["new"]["skills"] == [{"type": "custom", "skill_id": "note", "version": "1"}]


def test_external_managed_removal_blocks_without_post():
    class Client:
        async def request(self, method, path, **kwargs):
            assert method == "GET"
            return {"id": "writer", "version": 2, "skills": []}
    bindings = {"note-craft": {"id": "note", "version": "1", "agents": ["writer"]}}
    with pytest.raises(ValueError, match="removed externally"):
        asyncio.run(update_agent(Client(), FakeEffects(), "writer", bindings, bindings, {}, set(), "release"))


def test_conflict_refetches_and_merges_new_system():
    cloud = {"id": "writer", "version": 2, "system": "first", "skills": [
        {"type": "custom", "skill_id": "note", "version": "1"}]}
    attempts = []
    class Client:
        async def request(self, method, path, **kwargs):
            if method == "POST":
                from teamEvolver.managed_agents import MAError
                attempts.append(kwargs["json"])
                if len(attempts) == 1:
                    cloud.update(version=3, system="concurrent edit")
                    raise MAError(409, "VersionConflict")
                cloud.update(kwargs["json"], version=4)
            return copy.deepcopy(cloud)
    old = {"note-craft": {"id": "note", "version": "1", "agents": ["writer"]}}
    new = {"note-craft": {"id": "note", "version": "2", "agents": ["writer"]}}
    asyncio.run(update_agent(Client(), FakeEffects(), "writer", old, new, {}, set(), "release"))
    assert len(attempts) == 2 and attempts[1]["system"] == "concurrent edit"
    assert cloud["skills"][0]["version"] == "2"


def test_unknown_create_cleanup_requires_unique_match_and_never_sweeps_prefix():
    from team_ma.recovery import delete_owned, recover_owned
    requested = []
    class Client:
        async def listing(self, path, **kwargs):
            return [{"id": "one", "name": "owned"}, {"id": "two", "name": "owned"}]
        async def request(self, method, path, **kwargs):
            requested.append((method, path))
            return {}
    effect = {"kind": "replay-agent", "intent": {"name": "owned"}, "remote_id": ""}
    assert asyncio.run(recover_owned(Client(), effect)) is None
    assert requested == []
    assert asyncio.run(delete_owned(Client(), "replay-agent", "one"))
    assert requested == [("DELETE", "agents/one")]


def test_transport_failure_after_http_rejection_keeps_unknown_intent():
    # This contract is checked through PG elsewhere; MA protocol errors must be
    # typed so a lost response is distinguishable from a definite rejection.
    from teamEvolver.managed_agents import MAClient, MAError
    async def run():
        def timeout(request):
            raise httpx.ReadTimeout("lost", request=request)
        async with MAClient("https://example/api/v3", "key", transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                await client.request("POST", "agents")
    asyncio.run(run())


def test_owned_skill_cleanup_deletes_every_version_before_the_root():
    from team_ma.recovery import delete_owned
    requested = []
    class Client:
        async def listing(self, path):
            return [{"version": "1"}, {"version": "2"}]
        async def request(self, method, path, **kwargs):
            requested.append((method, path))
            return {}
    assert asyncio.run(delete_owned(Client(), "replay-skill", "owned"))
    assert requested == [("DELETE", "skills/owned/versions/1"), ("DELETE", "skills/owned/versions/2"),
                         ("DELETE", "skills/owned")]
