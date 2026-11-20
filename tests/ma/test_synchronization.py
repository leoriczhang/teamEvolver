import asyncio
import copy

import httpx
import pytest

from team_ma.runtime_binding import release_environment_name
from team_ma.synchronization import ensure_release_environment, update_agent
from tests.support.managed_agents import FakeEffects


@pytest.mark.parametrize("default_user", ["", "employee42"])
def test_pull_authentication_does_not_require_a_default_submitter(default_user):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from team_ma.synchronization import pull

    @asynccontextmanager
    async def tenant_tx(tenant):
        assert tenant == "alpha"
        yield None

    async def secret(conn, *, secret_ref):
        return "tenant-test-token"

    def respond(request):
        assert request.url.params["user_id"] == (default_user or "ma-sync")
        assert request.headers["authorization"] == "Bearer tenant-test-token"
        return httpx.Response(200, json={"status": "ok", "total": 0, "skills": []}, headers={"etag": "version"})

    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx), extras={
        "secret_repo": SimpleNamespace(resolve=secret),
        "ma_pull_client": lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs),
    })
    config = {"team_evolver_url": "https://te.example", "tenant_token_ref": "ref", "default_user_id": default_user}
    manifest, etag = asyncio.run(pull(services, "alpha", config))
    assert manifest["skills"] == [] and etag == "version"
    assert config["default_user_id"] == default_user


def test_release_environment_name_is_readable_unique_and_bounded():
    base = {"name": "codingvideo-dev-runtime"}
    config = {"type": "cloud", "setup_script": "one"}
    name = release_environment_name(base, "codingvideo", config)
    assert name.startswith("codingvideo-dev-runtime-")
    assert len(name) <= 64
    assert name == release_environment_name(base, "codingvideo", config)
    assert name != release_environment_name(base, "codingvideo", {**config, "setup_script": "two"})
    assert name != release_environment_name(base, "another-connection", config)
    long_name = release_environment_name({"name": "a" * 64}, "codingvideo", config)
    assert len(long_name) == 64 and long_name.startswith("a" * 51 + "-")


def test_public_release_exposes_stable_alias_and_actual_environment_name():
    from team_ma.api import _release_public
    record = {
        "release_id": "release", "environment_id": "env-new",
        "environment": {"name": "te-release-legacy", "config": {"setup_script": "private"}},
        "base_environment": {"name": "codingvideo-dev-runtime", "config": {"setup_script": "private"}},
    }
    public = _release_public(record)
    assert public["environment_alias"] == "codingvideo-dev-runtime"
    assert public["environment_name"] == "te-release-legacy"
    assert "private" not in str(public)


def test_release_environment_create_reuse_and_transport_recovery():
    from teamEvolver.managed_agents import MAError
    base = {"name": "codingvideo-dev-runtime", "config": {"type": "cloud", "setup_script": "old"}}
    current = {"id": "env-old", **base}
    runtime = {"type": "cloud", "setup_script": "new"}
    expected_name = release_environment_name(base, "codingvideo", runtime)

    class Client:
        def __init__(self):
            self.remote = {}
            self.posts = 0
            self.lose_response = False

        async def request(self, method, path, **kwargs):
            if method == "POST":
                self.posts += 1
                payload = kwargs["json"]
                if payload["name"] in {row["name"] for row in self.remote.values()}:
                    raise MAError(409, "Conflict")
                ident = f"env-{self.posts}"
                self.remote[ident] = {"id": ident, **payload}
                if self.lose_response:
                    raise MAError(503, "TransportOutcomeUnknown")
                return self.remote[ident]
            return self.remote[path.split("/")[1]]

        async def listing(self, path):
            assert path == "environments"
            return list(self.remote.values())

    class RecoveringEffects(FakeEffects):
        async def apply(self, effect_id, kind, intent, send, reconcile=None):
            try:
                receipt = await send()
            except MAError as error:
                assert error.code == "TransportOutcomeUnknown"
                receipt = await reconcile({})
                if not receipt:
                    raise ValueError("unreconciled") from error
            self.records.append({"effect_id": effect_id, "kind": kind, "intent": intent, "receipt": receipt})
            return receipt

    client = Client()
    effects = RecoveringEffects()
    created = asyncio.run(ensure_release_environment(
        client, effects, "release-one", "codingvideo", base, current, runtime,
    ))
    assert created["name"] == expected_name and client.posts == 1
    unchanged = asyncio.run(ensure_release_environment(
        client, effects, "release-one", "codingvideo", base, created, runtime,
    ))
    assert unchanged == created and client.posts == 1

    normalized = {
        **created,
        "config": {**runtime, "spec": {"cpu": 1}, "template_id": "ma-default"},
    }
    client.remote[created["id"]] = normalized
    reused = asyncio.run(ensure_release_environment(
        client, effects, "release-one", "codingvideo", base, normalized, runtime,
    ))
    assert reused == normalized and client.posts == 1

    client.lose_response = True
    recovered = asyncio.run(ensure_release_environment(
        client, effects, "release-two", "codingvideo", base, created, {**runtime, "setup_script": "next"},
    ))
    assert recovered["id"] == "env-2" and client.posts == 2

    client.remote["conflict"] = {"id": "conflict", "name": expected_name, "config": {"type": "cloud"}}
    with pytest.raises(MAError, match="Conflict"):
        asyncio.run(ensure_release_environment(
            client, FakeEffects(), "release-three", "codingvideo", base, current, runtime,
        ))


def test_release_environment_recovery_rejects_same_name_with_different_config():
    from team_ma.effects import UnknownEffect
    base = {"name": "codingvideo-dev-runtime", "config": {"type": "cloud", "setup_script": "old"}}
    current = {"id": "env-old", **base}
    runtime = {"type": "cloud", "setup_script": "new"}
    name = release_environment_name(base, "codingvideo", runtime)

    class Client:
        async def listing(self, path):
            return [{"id": "foreign", "name": name}]

        async def request(self, method, path, **kwargs):
            assert (method, path) == ("GET", "environments/foreign")
            return {"id": "foreign", "name": name, "config": {"type": "cloud", "setup_script": "foreign"}}

    class ReconcileEffects:
        async def apply(self, effect_id, kind, intent, send, reconcile=None):
            if not await reconcile({}):
                raise UnknownEffect("name belongs to another configuration")
            raise AssertionError("foreign Environment must not be adopted")

    with pytest.raises(UnknownEffect, match="another configuration"):
        asyncio.run(ensure_release_environment(
            Client(), ReconcileEffects(), "release", "codingvideo", base, current, runtime,
        ))


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
