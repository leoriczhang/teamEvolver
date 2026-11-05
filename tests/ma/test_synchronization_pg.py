import asyncio
import base64
import copy

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from team_ma.synchronization import cleanup, reconcile_connection
from team_skills.config import BundleLimits
from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient, MAError
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.managed_agents import skill_zip

pytestmark = pytest.mark.postgres


def test_partial_external_result_is_reconciled_and_system_edits_survive(pg_env):
    repo = MARepository(pg_env.tx)
    lib = SkillLibraryRepository(pg_env.tx)
    secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig())
    services.extras["secret_repo"] = secrets
    new_files = {"SKILL.md": b"---\nname: note-craft\ndescription: Useful notes\n---\nImproved\n"}
    old_files = {"SKILL.md": b"---\nname: note-craft\ndescription: Useful notes\n---\nOriginal\n"}
    tree, old_tree = bundle_tree_sha256(new_files), bundle_tree_sha256(old_files)
    cloud = {"id": "env", "config": {"type": "cloud", "env": {"KEEP": "yes"}}}
    agents = {
        "root": {"id": "root", "version": 1, "name": "editor", "system": "Keep system", "skills": [],
                 "multiagent": {"type": "coordinator", "agents": [{"type": "agent", "id": "writer", "version": 1}]}},
        "writer": {"id": "writer", "version": 1, "name": "writer", "system": "External edited system",
                   "metadata": {"owner": "business"}, "skills": [
                       {"type": "custom", "skill_id": "skill", "version": "1"},
                       {"type": "ark", "skill_id": "preset"}]},
    }
    uploads, agent_posts, versions = [], [], ["1"]
    lose_once = [True]
    rollback = [False]

    def ma_response(request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "skills/skill/versions":
            if request.method == "POST":
                uploads.append(request.content)
                versions.append("2")
                return httpx.Response(200, json={"id": "version-2", "skill_id": "skill", "version": "2"})
            return httpx.Response(200, json={"data": [{"version": v} for v in versions]})
        if path.endswith("/content"):
            value = "Improved" if "/2/" in path else "Original"
            return httpx.Response(200, content=skill_zip(body=value))
        if path.startswith("environments/"):
            return httpx.Response(200, json=cloud)
        if path.startswith("agents/"):
            ident = path.split("/")[1]
            if request.method == "POST":
                import json
                value = json.loads(request.content)
                agents[ident] = {"id": ident, **value, "version": agents[ident]["version"] + 1}
                agent_posts.append(ident)
                if ident == "writer" and lose_once[0]:
                    lose_once[0] = False
                    raise httpx.ReadTimeout("response lost after commit", request=request)
            return httpx.Response(200, json=copy.deepcopy(agents[ident]))
        raise AssertionError(path)

    def pull_response(request):
        assert request.headers["authorization"] == "Bearer tevt_test"
        return httpx.Response(200, headers={"etag": '"published"'}, json={
            "status": "ok", "total": 1, "skills": [{"name": "note-craft", "version": 3 if rollback[0] else 2,
                                                   "tree_sha256": old_tree if rollback[0] else tree,
                                                   "files": [{"path": path, "content_b64":
                                                              base64.b64encode(data).decode()}
                                                             for path, data in
                                                             (old_files if rollback[0] else new_files).items()]}],
        })
    services.extras["ma_pull_client"] = lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(pull_response), **kw,
    )

    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            async with pg_env.tx.tenant_tx("alpha") as conn:
                ark = await secrets.store(conn, tenant_id="alpha", plaintext="ark")
                pull = await secrets.store(conn, tenant_id="alpha", plaintext="tevt_test")
                config = validate_config({"display_name": "Example", "entry_agent_id": "root", "environment_id": "env",
                                          "default_user_id": "operator", "ma_secret_ref": ark.ref,
                                          "tenant_token_ref": pull.ref})
                await repo.save(conn, tenant_id="alpha", connection_id="connection", config=config)
                await repo.bind(conn, tenant_id="alpha", connection_id="connection", kind="skill", name="note-craft",
                                record={"id": "skill", "version": "1", "tree_sha256": old_tree, "agents": ["writer"],
                                        "team_version": 1})
                await lib.commit_bundle(conn, tenant_id="alpha", name="note-craft", bundle=old_files,
                                        limits=BundleLimits(), mutation_id="original")
                await lib.commit_bundle(conn, tenant_id="alpha", name="note-craft", bundle=new_files,
                                        limits=BundleLimits(), mutation_id="admin-edit")
                await repo.save_release(
                    conn, tenant_id="alpha", connection_id="connection", release_id="initial", manifest_hash="initial",
                    status="applied", record={"environment": cloud, "environment_id": "env",
                                             "agent_graph": {"entry_agent_id": "root",
                                                             "agents": copy.deepcopy(agents)}},
                )
                await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.reconcile",
                              operation_id="sync")
                task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300, kinds=["ma.reconcile"])
                connection = await repo.connection(conn, "connection")
                operation = await repo.operation(conn, "sync")
            ma_client = MAClient("https://ma.example/api/v3", "ark", transport=httpx.MockTransport(ma_response))
            async with ma_client as client:
                with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                    await reconcile_connection(task, services, client, connection, operation)
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    assert (await repo.applied(conn, "connection"))["release_id"] == "initial"
                    operation = await repo.operation(conn, "sync")
                await reconcile_connection(task, services, client, connection, operation)
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    release = await repo.applied(conn, "connection")
                    assert release["record"]["entry_agent_version"] == "2"
                    assert release["record"]["readback_verified"]
                rollback[0] = True
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    await lib.commit_bundle(conn, tenant_id="alpha", name="note-craft", bundle=old_files,
                                            limits=BundleLimits(), mutation_id="rollback")
                    await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.reconcile",
                                  operation_id="rollback-sync")
                    rollback_task = await services.tasks.claim(conn, owner_id="worker", lease_seconds=300,
                                                               kinds=["ma.reconcile"])
                    operation = await repo.operation(conn, "rollback-sync")
                await reconcile_connection(rollback_task, services, client, connection, operation)
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    restored = await repo.applied(conn, "connection")
                    assert restored["release_id"] != release["release_id"]
                    assert restored["record"]["skills"]["note-craft"]["version"] == "1"
                    assert restored["record"]["skills"]["note-craft"]["team_version"] == 3
            assert len(uploads) == 1
            assert agent_posts == ["writer", "root", "writer", "root"]  # lost POST and rollback never duplicate uploads
            assert agents["writer"]["system"] == "External edited system"
            assert agents["writer"]["metadata"] == {"owner": "business"}
            assert agents["writer"]["skills"][1] == {"type": "ark", "skill_id": "preset"}
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_cleanup_retires_events_only_after_their_owned_session_is_deleted(pg_env):
    repo = MARepository(pg_env.tx)
    services = build_role_services(role="api", tx=pg_env.tx, config=TeamEvolverConfig())
    deleted = []

    def response(request):
        assert request.url.path.endswith("/sessions/session")
        if request.method == "DELETE":
            deleted.append("session")
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "session", "status": "idle"})

    async def run():
        config = validate_config({"display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
                                  "default_user_id": "operator", "ma_secret_ref": "ark", "tenant_token_ref": "pull"})
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
            connection = await repo.connection(conn, "one")
            for ident, owner, kind, intent, status, remote in (
                ("session", "owned", "replay-session", {}, "cleanup_pending", "session"),
                ("event", "owned", "replay-event", {"session_id": "session"}, "cleanup_pending", ""),
                ("active", "active", "replay-event", {"session_id": "other"}, "confirmed", ""),
                ("foreign", "foreign", "replay-event", {"session_id": "session"}, "confirmed", ""),
            ):
                await repo.prepare_effect(conn, tenant_id="alpha", connection_id="one", effect_id=ident,
                                          owner_id=owner, kind=kind, intent=intent)
                await repo.confirm_effect(conn, ident, remote, {}, status=status)
            await repo.retire_session_events(conn, "one")
            assert (await repo.effect(conn, "event"))["status"] == "cleanup_pending"
        async with MAClient("https://ma.example/api/v3", "key",
                            transport=httpx.MockTransport(response)) as client:
            await cleanup(services, client, connection)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.effect(conn, "session"))["status"] == "cleaned"
            assert (await repo.effect(conn, "event"))["status"] == "cleaned"
            assert (await repo.effect(conn, "active"))["status"] == "confirmed"
            assert (await repo.effect(conn, "foreign"))["status"] == "confirmed"
        assert deleted == ["session"]

    asyncio.run(run())
