"""Cloud authority, native writes, historical bytes and stale-write protection."""
import asyncio
import copy
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from team_ma.commands import enqueue
from team_ma.discovery import discover_connection, read_skill_zip
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from team_ma.skill_writes import write_skill
from team_skills.config import BundleLimits
from team_skills.library.repository import SkillLibraryRepository
from team_skills.library.store import skill_scope
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.security import SecretBox, SecretRepository
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.managed_agents import skill_zip

pytestmark = pytest.mark.postgres


class NativeSkills:
    def __init__(self):
        self.archives = {1: skill_zip(body="Original"), 2: skill_zip(body="Cloud latest")}
        self.agent = {"id": "root", "name": "editor", "version": 1, "system": "External system remains",
                      "skills": [{"type": "custom", "skill_id": "remote-note", "version": "1"}]}
        self.posts = []
        self.lost_upload = False
        self.deleted = False
        self.new = None
        self.titles = {1: "note-craft", 2: "note-craft"}
        self.other_skills = {}

    def client(self, base="https://ma.example/api/v3", token="ark"):
        return MAClient(base, token, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "skills":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content,
            )
            archive = next(p.get_payload(decode=True) for p in message.iter_parts() if p.get_filename())
            item = read_skill_zip(archive)
            title = next(p.get_content() for p in message.iter_parts()
                         if p.get_param("name", header="content-disposition") == "display_title")
            self.new = {"id": "remote-new", "name": item.name, "latest_version": "1", "display_title": title}
            self.new_archive = archive
            self.posts.append(path)
            if self.lost_upload:
                self.lost_upload = False
                raise httpx.ReadTimeout("lost after remote commit", request=request)
            return httpx.Response(200, json=self.new)
        if path == "skills":
            return httpx.Response(200, json={"data": [
                *([self.new] if self.new else []), *(v[0] for v in self.other_skills.values()),
                {"id": "remote-note", "name": "note-craft", "display_title": self.titles[max(self.archives)]},
            ]})
        sid = path.split("/")[1] if path.startswith("skills/") else ""
        if sid in self.other_skills:
            meta, archive = self.other_skills[sid]
            if path.endswith("/content"):
                return httpx.Response(200, content=archive)
            if path.endswith("/versions"):
                return httpx.Response(200, json={"data": [{**meta, "skill_id": sid, "version": "1"}]})
            return httpx.Response(200, json={**meta, "skill_id": sid, "version": "1"})
        if path == "skills/remote-new":
            return httpx.Response(200, json=self.new)
        if path == "skills/remote-new/versions":
            return httpx.Response(200, json={"data": [
                {"skill_id": "remote-new", "version": "1", "display_title": self.new["display_title"]},
            ]})
        if path == "skills/remote-new/versions/1":
            return httpx.Response(200, json={"skill_id": "remote-new", "version": "1",
                                            "display_title": self.new["display_title"]})
        if path == "skills/remote-new/versions/1/content":
            return httpx.Response(200, content=self.new_archive)
        if path == "skills/remote-note" and self.deleted:
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        if request.method == "DELETE" and path == "skills/remote-note":
            assert not self.agent["skills"], "Deletion must detach before DELETE"
            assert not self.archives, "Native versions must be removed before deleting the Skill"
            self.deleted = True
            return httpx.Response(200, json={})
        if request.method == "DELETE" and path.startswith("skills/remote-note/versions/"):
            version = int(path.split("/")[3])
            assert version == min(self.archives), "Delete native versions from oldest to newest"
            del self.archives[version]
            return httpx.Response(200, json={})
        if request.method == "POST":
            self.posts.append(path)
            if path == "skills/remote-note/versions":
                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content,
                )
                archive = next(p.get_payload(decode=True) for p in message.iter_parts() if p.get_filename())
                read_skill_zip(archive)
                version = max(self.archives) + 1
                self.archives[version] = archive
                self.titles[version] = next(p.get_content() for p in message.iter_parts()
                                           if p.get_param("name", header="content-disposition") == "display_title")
                if self.lost_upload:
                    self.lost_upload = False
                    raise httpx.ReadTimeout("lost after remote commit", request=request)
                return httpx.Response(200, json={"id": "skillver-" + str(version),
                                                "skill_id": "remote-note", "version": str(version)})
            if path == "agents/root":
                value = json.loads(request.content)
                self.agent = {**value, "id": "root", "version": self.agent["version"] + 1}
        if path == "agents/root":
            return httpx.Response(200, json=copy.deepcopy(self.agent))
        if path == "environments/env":
            return httpx.Response(200, json={"id": "env", "name": "Business", "config": {"type": "cloud"}})
        if path == "skills/remote-note":
            return httpx.Response(200, json={"id": "remote-note", "name": "note-craft",
                                            "display_title": self.titles[max(self.archives)],
                                            "latest_version": str(max(self.archives)), "updated_at": 1794990000})
        if path == "skills/remote-note/versions":
            return httpx.Response(200, json={"data": [
                {"skill_id": "remote-note", "version": str(v), "created_at": "2026-11-18T01:00:00Z"}
                for v in sorted(self.archives, reverse=True)]})
        if path.startswith("skills/remote-note/versions/"):
            version = int(path.split("/")[3])
            if version not in self.archives:
                return httpx.Response(404, json={"error": {"code": "NotFound"}})
            if path.endswith("/content"):
                return httpx.Response(200, content=self.archives[version])
            return httpx.Response(200, json={"skill_id": "remote-note", "version": str(version),
                                            "display_title": self.titles[version],
                                            "created_at": "2026-11-18T01:00:00Z"})
        raise AssertionError((request.method, path))


async def setup(env, remote):
    services = build_role_services(role="api", tx=env.tx, config=TeamEvolverConfig())
    services.extras["secret_repo"] = SecretRepository(env.tx, SecretBox("test-key"))
    services.extras["ma_client_factory"] = remote.client
    repo = MARepository(env.tx)
    async with env.tx.tenant_tx("alpha") as conn:
        secret = await services.extras["secret_repo"].store(conn, tenant_id="alpha", plaintext="ark")
        config = validate_config({
            "display_name": "Cloud notes", "entry_agent_id": "root", "environment_id": "env",
            "ma_base_url": "https://ma.example/api/v3", "ma_secret_ref": secret.ref,
            "tenant_token_ref": secret.ref,
        })
        await repo.save(conn, tenant_id="alpha", connection_id="notes", config=config)
        await enqueue(conn, services, tenant_id="alpha", connection_id="notes", kind="ma.discover",
                      operation_id="discover")
        task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.discover"])
        connection = await repo.connection(conn, "notes")
        operation = await repo.operation(conn, "discover")
    async with remote.client() as client:
        result = await discover_connection(task, services, client, connection, operation)
    return services, repo, result


def test_cloud_latest_is_authority_without_importing_a_team_version(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, result = await setup(pg_env, remote)
            assert result["managed_skill_count"] == 1
            async with pg_env.tx.tenant_tx("alpha") as conn:
                assert await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skill_versions") == 0
                resource = (await repo.resources(conn, "notes", kind="skill"))[0]["record"]
                assert resource["latest_version"] == "2" and resource["bound_version"] == "1"
            async with skill_scope(services, "alpha", versions={"note-craft": [1]}):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    library = SkillLibraryRepository(pg_env.tx)
                    head, bundle = await library.read_current_bundle(conn, "note-craft")
                    assert head["version"] == 2 and head["skill_id"] == "remote-note"
                    assert b"Cloud latest" in bundle["SKILL.md"]
                    original = await library.read_version_bundle(conn, "note-craft", 1)
                    assert b"Original" in original["SKILL.md"]
                    assert [v["version"] for v in await library.list_versions(conn, "note-craft")] == [2, 1]
                    assert all(v["provenance"]["kind"] == "unknown"
                               for v in await library.list_versions(conn, "note-craft"))
            assert not remote.posts
            async with skill_scope(services, "beta"):
                async with pg_env.tx.tenant_tx("beta") as conn:
                    assert await SkillLibraryRepository(pg_env.tx).list_skills(conn) == []
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_lost_upload_is_reconciled_once_and_rollback_creates_a_native_version(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            library = SkillLibraryRepository(pg_env.tx)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    _, files = await library.read_current_bundle(conn, "note-craft")
                    files["SKILL.md"] += b"\nReviewed\n"
                    await library.commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="lost",
                        limits=BundleLimits(), base_version=2,
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, task.task_id)
                    connection = await repo.connection(conn, "notes")
            from teamEvolver.managed_agents import MAError
            remote.lost_upload = True
            async with remote.client() as client:
                with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                    await write_skill(task, services, client, connection, operation)
                restored = await write_skill(task, services, client, connection, operation)
                assert restored["version"] == 3
            assert remote.posts.count("skills/remote-note/versions") == 1
            async with skill_scope(services, "alpha", versions={"note-craft": [1]}):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    await library.commit_rollback(
                        conn, tenant_id="alpha", name="note-craft", target_version=1,
                        mutation_id="rollback",
                    )
                    rollback = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, rollback.task_id)
            async with remote.client() as client:
                result = await write_skill(rollback, services, client, connection, operation)
            assert result["version"] == 4 and result["restored_from"] == 1
            assert b"Original" in read_skill_zip(remote.archives[4]).files["SKILL.md"]
            assert b"Reviewed" in read_skill_zip(remote.archives[3]).files["SKILL.md"]
            assert remote.agent["skills"][0]["version"] == "4"
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_delete_detaches_cloud_skill_and_does_not_revive_a_local_import(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            library = SkillLibraryRepository(pg_env.tx)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    await library.commit_delete(conn, tenant_id="alpha", name="note-craft",
                                         mutation_id="delete")
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, task.task_id)
                    connection = await repo.connection(conn, "notes")
            async with remote.client() as client:
                result = await write_skill(task, services, client, connection, operation)
            assert result["deleted"] and remote.deleted and not remote.agent["skills"]
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    assert await library.list_skills(conn) == []
                    assert await library.get_skill(conn, "note-craft") is None
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_new_skill_uses_native_resource_id_and_idempotency_survives_creation(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            library = SkillLibraryRepository(pg_env.tx)
            files = read_skill_zip(skill_zip("new-notes", "New skill")).files
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    commit = await library.commit_bundle(
                        conn, tenant_id="alpha", name="new-notes", bundle=files, mutation_id="new",
                        limits=BundleLimits(), base_version=0,
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    connection = await repo.connection(conn, "notes")
                    operation = await repo.operation(conn, task.task_id)
            async with remote.client() as client:
                result = await write_skill(task, services, client, connection, operation)
            assert result["skill_id"] == "remote-new" and result["version"] == 1
            assert remote.new["display_title"] == "new-notes"
            assert remote.agent["skills"] == [{"type": "custom", "skill_id": "remote-note", "version": "1"}]
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    repeated = await library.commit_bundle(
                        conn, tenant_id="alpha", name="new-notes", bundle=files, mutation_id="new",
                        limits=BundleLimits(), base_version=0,
                    )
                    assert repeated.record["job"]["job_id"] == commit.record["job"]["job_id"]
                    assert await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skill_versions") == 0
            assert remote.posts.count("skills") == 1
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


@pytest.mark.parametrize("recovery", ["unique", "different-content", "ambiguous", "absent", "legacy"])
def test_new_skill_recovers_lost_creation_without_reusing_existing_resources(pg_env, recovery):
    async def run():
        from team_ma.effects import UnknownEffect
        from team_ma.schemas import digest
        from team_skills.library.bundle import bundle_tree_sha256
        from teamEvolver.managed_agents import MAError

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            archive = skill_zip("new-notes", "New skill")
            files = read_skill_zip(archive).files
            meta = {"id": "preexisting", "name": "new-notes", "display_title": "new-notes", "latest_version": "1"}
            remote.other_skills["preexisting"] = (meta, archive)
            services, repo, _ = await setup(pg_env, remote)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    await SkillLibraryRepository(pg_env.tx).commit_bundle(
                        conn, tenant_id="alpha", name="new-notes", bundle=files, mutation_id="new-lost",
                        limits=BundleLimits(), base_version=0,
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    connection = await repo.connection(conn, "notes")
                    operation = await repo.operation(conn, task.task_id)
                    if recovery == "legacy":
                        title = "te-" + digest([task.tenant_id, task.task_id])[:32]
                        await repo.prepare_effect(
                            conn, tenant_id="alpha", connection_id="notes", effect_id=task.task_id + ":skill",
                            owner_id=task.task_id, kind="cloud-skill-version",
                            intent={"skill_id": "", "name": "new-notes", "base_version": 0,
                                    "tree_sha256": bundle_tree_sha256(files), "title": title},
                        )
                        remote.new = {**meta, "id": "remote-new", "display_title": title}
                        remote.new_archive = archive
            async with remote.client() as client:
                if recovery != "legacy":
                    remote.lost_upload = True
                    with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                        await write_skill(task, services, client, connection, operation)
                    assert remote.new["display_title"] == "new-notes"
                if recovery in {"ambiguous", "different-content"}:
                    remote.other_skills["concurrent"] = (
                        {**meta, "id": "concurrent"},
                        archive if recovery == "ambiguous" else skill_zip("new-notes", "Different content"),
                    )
                if recovery == "absent":
                    remote.new = None
                if recovery in {"ambiguous", "absent"}:
                    with pytest.raises(UnknownEffect):
                        await write_skill(task, services, client, connection, operation)
                else:
                    result = await write_skill(task, services, client, connection, operation)
                    assert result["skill_id"] == "remote-new" and result["version"] == 1
                assert remote.posts.count("skills") == (0 if recovery == "legacy" else 1)
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


@pytest.mark.parametrize("readback_fails", [False, "binding", "title"])
def test_new_candidate_binds_entry_agent_before_marking_published(pg_env, readback_fails):
    async def run():
        from team_skills.candidates.repository import CandidateRepository

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            remote.agent["skills"] = []
            if readback_fails:
                respond = remote.respond

                def ignore_binding(request):
                    if (readback_fails == "binding" and request.method == "POST"
                            and request.url.path.endswith("/agents/root")):
                        return httpx.Response(200, json=copy.deepcopy(remote.agent))
                    if (readback_fails == "title" and request.method == "GET"
                            and request.url.path.endswith("/skills/remote-new/versions/1")):
                        return httpx.Response(200, json={
                            "skill_id": "remote-new", "version": "1", "display_title": "wrong-title",
                        })
                    return respond(request)

                remote.respond = ignore_binding
            services, repo, _ = await setup(pg_env, remote)
            candidates = CandidateRepository(pg_env.tx)
            files = read_skill_zip(skill_zip("new-notes", "New skill")).files
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    item = await candidates.create(
                        conn, tenant_id="alpha", skill_name="new-notes", base_version=0,
                        proposed_action="create", bundle=files, limits=BundleLimits(), detail={},
                    )
                    commit = await SkillLibraryRepository(pg_env.tx).commit_bundle(
                        conn, tenant_id="alpha", name="new-notes", bundle=files, mutation_id="new-candidate",
                        limits=BundleLimits(), base_version=0, actor="admin", source="candidate-review",
                        metadata={"candidate_id": item["candidate_id"], "revision": 1},
                    )
                    await candidates.transition(
                        conn, tenant_id="alpha", candidate_id=item["candidate_id"], to_status="validating",
                        actor="admin", expected_revision=1, validate_task_id=commit.record["job"]["job_id"],
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    connection = await repo.connection(conn, "notes")
                    operation = await repo.operation(conn, task.task_id)
                    assert (await candidates.require(conn, item["candidate_id"]))["status"] == "validating"
            async with remote.client() as client:
                if readback_fails:
                    message = ("Agent reference readback mismatch" if readback_fails == "binding"
                               else "display title readback mismatch")
                    with pytest.raises(ValueError, match=message):
                        await write_skill(task, services, client, connection, operation)
                    async with pg_env.tx.tenant_tx("alpha") as conn:
                        assert (await candidates.require(conn, item["candidate_id"]))["status"] == "validating"
                    return
                result = await write_skill(task, services, client, connection, operation)
            assert remote.agent["skills"] == [{"type": "custom", "skill_id": "remote-new", "version": "1"}]
            assert result["deployment_status"] == "bound" and result["bound_agents"] == ["root"]
            async with pg_env.tx.tenant_tx("alpha") as conn:
                assert (await candidates.require(conn, item["candidate_id"]))["status"] == "published"
            assert remote.posts.count("skills") == 1
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_explicit_empty_candidate_targets_require_a_member_before_upload(pg_env):
    async def run():
        from team_skills.library.repository import SkillMutationConflict

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            async with pg_env.tx.tenant_tx("alpha") as conn:
                await repo.create_binding(
                    conn, tenant_id="alpha", connection_id="notes", name="new-notes",
                    record={"name": "new-notes", "agents": [], "desired_agents": []},
                )
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    with pytest.raises(SkillMutationConflict, match="at least one MA member"):
                        await SkillLibraryRepository(pg_env.tx).commit_bundle(
                            conn, tenant_id="alpha", name="new-notes",
                            bundle=read_skill_zip(skill_zip("new-notes", "New skill")).files,
                            mutation_id="empty-choice", limits=BundleLimits(), base_version=0,
                            source="candidate-review", metadata={"candidate_id": "new", "revision": 1},
                        )
                    assert not await conn.fetchval(
                        f"SELECT count(*) FROM {pg_env.schema}.ma_operations WHERE kind='ma.skill'")
            assert remote.posts == []
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


@pytest.mark.parametrize("readback_fails", [False, True])
def test_forced_cloud_candidate_preserves_review_and_waits_for_readback(pg_env, monkeypatch, readback_fails):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Forced publication must not assess or freeze replay inputs")

    monkeypatch.setattr("team_skills.evolution.model.model_for", forbidden)

    async def run():
        from team_skills.candidates.repository import CandidateRepository
        from team_skills.config import load_skills_config
        from team_skills.evolution.candidate_pipeline import run_validation, submit_validation
        from team_skills.evolution.history import candidate_payload

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            services.extras["ma_freeze_replay"] = forbidden
            candidates = CandidateRepository(pg_env.tx)
            review = {"candidate_revision": 1, "recommended_publish": False,
                      "replay": {"validation_method": "candidate_execution", "verdict": "inconclusive",
                                 "reason": "Historical execution snapshot incomplete"}}
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    item = await candidates.create(
                        conn, tenant_id="alpha", skill_name="new-notes", base_version=0,
                        proposed_action="create", bundle=read_skill_zip(skill_zip("new-notes", "New skill")).files,
                        limits=BundleLimits(),
                    )
                    await candidates.record_verdict(
                        conn, tenant_id="alpha", candidate_id=item["candidate_id"], revision=1,
                        verdict="inconclusive", evaluation=review,
                    )
                    config = await load_skills_config(services.extras["config_repo"], conn)
                    await submit_validation(
                        conn, services, tenant_id="alpha", candidate_id=item["candidate_id"],
                        command_id="force-cloud", config=config, actor="admin", revision=1, mode="force",
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300,
                                                      kinds=["skill.validate"])
                await run_validation(task, services)
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    pending = await candidates.require(conn, item["candidate_id"])
                    assert pending["status"] == "validating" and not pending["published_version"]
                    assert pending["evaluation"] == review and not remote.new
                    assert (await candidate_payload(conn, pg_env.tx, pending, compact=True))["publication_pending"]
                    cloud_task = await services.tasks.claim(
                        conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"],
                    )
                    connection = await repo.connection(conn, "notes")
                    operation = await repo.operation(conn, cloud_task.task_id)
            if readback_fails:
                respond = remote.respond

                def ignore_binding(request):
                    if request.method == "POST" and request.url.path.endswith("/agents/root"):
                        return httpx.Response(200, json=copy.deepcopy(remote.agent))
                    return respond(request)

                remote.respond = ignore_binding
            async with remote.client() as client:
                if readback_fails:
                    with pytest.raises(ValueError, match="Agent reference readback mismatch"):
                        await write_skill(cloud_task, services, client, connection, operation)
                else:
                    await write_skill(cloud_task, services, client, connection, operation)
            async with pg_env.tx.tenant_tx("alpha") as conn:
                final = await candidates.require(conn, item["candidate_id"])
                assert final["evaluation"] == review and final["verdict"] == "inconclusive"
                assert final["status"] == ("validating" if readback_fails else "published")
                assert final["decision"]["mode"] == "force"
                assert final["decision"]["validation_overridden"] is True
        finally:
            reset_current_tenant(token)

    asyncio.run(run())


@pytest.mark.parametrize("title,lost", [
    ("note-craft", False), ("My custom notes", False), ("te-" + "a" * 32, False), ("te-" + "a" * 32, True),
])
def test_unchanged_cloud_content_still_applies_and_verifies_agent_bindings(pg_env, title, lost):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            remote.titles[2] = title
            services, repo, _ = await setup(pg_env, remote)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    lib = SkillLibraryRepository(pg_env.tx)
                    _, files = await lib.read_current_bundle(conn, "note-craft")
                    await lib.commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="unchanged",
                        limits=BundleLimits(), base_version=2,
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, task.task_id)
                    connection = await repo.connection(conn, "notes")
            async with remote.client() as client:
                if lost:
                    from teamEvolver.managed_agents import MAError
                    remote.lost_upload = True
                    with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                        await write_skill(task, services, client, connection, operation)
                result = await write_skill(task, services, client, connection, operation)
            repair = title.startswith("te-")
            assert remote.agent["skills"][0]["version"] == ("3" if repair else "2")
            assert result["status"] == ("published" if repair else "unchanged")
            assert result["deployment_status"] == "bound"
            assert remote.posts == (["skills/remote-note/versions", "agents/root"] if repair else ["agents/root"])
            if repair:
                assert remote.titles[3] == "note-craft"
                assert read_skill_zip(remote.archives[2]).files == read_skill_zip(remote.archives[3]).files
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


@pytest.mark.parametrize("agents", [[], ["root"]])
def test_binding_api_applies_existing_version_without_upload_or_reset(pg_env, agents):
    async def run():
        from fastapi import FastAPI

        from team_ma.api import create_router

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            async with pg_env.tx.tenant_tx("alpha") as conn:
                original = (await repo.all_resources(conn, "notes", kind="skill"))[0]
            app = FastAPI()
            @app.middleware("http")
            async def identity(request, call_next):
                request.state.console_user = {"service_root": True, "id": "admin"}
                return await call_next(request)
            app.include_router(create_router(services))
            body = {"expected_revision": original["revision"], "agent_ids": agents}
            url = "/api/ma/connections/notes/resources/note-craft/bindings"
            headers = {"Idempotency-Key": "bind-note"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te") as client:
                response = await client.put(url, json=body, headers=headers)
                assert response.status_code == 202, response.text
                job = response.json()
                repeated = await client.put(url, json=body, headers=headers)
                assert repeated.status_code == 202 and repeated.json()["job_id"] == job["job_id"]
                conflict = await client.put(url, json={**body, "agent_ids": ["root"] if not agents else []},
                                            headers=headers)
                assert conflict.status_code == 409
                stale = await client.put(url, json=body)
                assert stale.status_code == 409
            assert remote.posts == []
            async with pg_env.tx.tenant_tx("alpha") as conn:
                pending = (await repo.all_resources(conn, "notes", kind="skill"))[0]
                assert pending["record"]["agents"] == original["record"]["agents"]
                task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                operation = await repo.operation(conn, task.task_id)
                connection = await repo.connection(conn, "notes")
            async with remote.client() as client:
                result = await write_skill(task, services, client, connection, operation)
            assert result["version"] == 2
            assert result["deployment_status"] == ("bound" if agents else "unbound")
            assert remote.agent["skills"] == (
                [{"type": "custom", "skill_id": "remote-note", "version": "2"}] if agents else [])
            assert remote.posts == ["agents/root"]
            async with pg_env.tx.tenant_tx("alpha") as conn:
                current = (await repo.all_resources(conn, "notes", kind="skill"))[0]["record"]
                assert current["agents"] == agents
                assert current["evolution_reset_at"] == original["record"]["evolution_reset_at"]
                assert not await repo.skill_version_receipts(conn, "notes", "remote-note")
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_skill_api_exposes_native_history_and_accepts_cloud_writes(pg_env):
    async def run():
        from fastapi import FastAPI

        from team_skills.api import create_router
        remote = NativeSkills()
        token = set_current_tenant(TenantContext("alpha"))
        try:
            services, _, _ = await setup(pg_env, remote)
            app = FastAPI()
            @app.middleware("http")
            async def identity(request, call_next):
                request.state.console_user = {"service_root": True, "id": "reviewer"}
                return await call_next(request)
            app.include_router(create_router(services))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te") as client:
                listing = await client.get("/api/skills")
                assert listing.status_code == 200
                head = listing.json()["skills"][0]
                assert (head["version"], head["connection_id"], head["bound_version"]) == (2, "notes", "1")
                original = await client.get("/api/skills/note-craft/versions/1")
                assert original.status_code == 200 and "Original" in original.json()["raw_md"]
                assert original.json()["provenance"]["kind"] == "unknown"
                assert [v["version"] for v in original.json()["history"]] == [2, 1]
                workspace = await client.get("/api/replay-lab/workspace?connection_id=notes")
                assert workspace.status_code == 200, workspace.text
                assert workspace.json()["skill_backend"] == "managed-agents"
                current = workspace.json()["skills"][0]
                assert (current["version"], current["connection_id"]) == (2, "notes")
                file = await client.get("/api/replay-lab/workspace/note-craft/file",
                                        params={"path": "SKILL.md", "connection_id": "notes"})
                assert file.status_code == 200, file.text
                assert file.json()["version"] == 2 and "Cloud latest" in file.json()["content"]
                detail = await client.get("/api/replay-lab/skills/note-craft?connection_id=notes")
                assert detail.status_code == 200 and "Cloud latest" in detail.json()["skill_md"]
                write = await client.put("/api/replay-lab/workspace/note-craft/file?connection_id=notes", json={
                    "path": "SKILL.md", "content": file.json()["content"] + "\nWorkbench edit",
                    "expected_sha256": file.json()["sha256"],
                }, headers={"Idempotency-Key": "workbench-edit"})
                assert write.status_code == 202, write.text
                assert write.json()["job_id"].startswith("ma-skill:")
                updated = await client.post("/api/skills", json={
                    "name": "note-craft", "skill_md": "---\nname: note-craft\ndescription: Notes\n---\nAPI edit",
                    "base_version": 2,
                }, headers={"Idempotency-Key": "api-edit"})
                assert updated.status_code == 202, updated.text
                assert updated.json()["job_id"].startswith("ma-skill:")
                assert updated.json()["result_url"].startswith("/api/ma/skill-jobs/")
            assert remote.posts == []
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_native_history_uses_verified_receipts_and_isolates_connections(pg_env):
    async def run():
        from team_skills.candidates.repository import CandidateRepository
        from team_skills.evolution.history import version_context
        from team_skills.library.bundle import encode_bundle_payload

        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            lib = SkillLibraryRepository(pg_env.tx)
            candidates = CandidateRepository(pg_env.tx)
            candidate_id = ""
            for index, source in enumerate(("console", "candidate-review"), 3):
                async with skill_scope(services, "alpha"):
                    async with pg_env.tx.tenant_tx("alpha") as conn:
                        _, bundle = await lib.read_current_bundle(conn, "note-craft")
                        baseline = encode_bundle_payload(bundle)
                        bundle["SKILL.md"] += f"\nChange {index}\n".encode()
                        if source == "candidate-review":
                            item = await candidates.create(
                                conn, tenant_id="alpha", skill_name="note-craft", base_version=3,
                                proposed_action="update", bundle=bundle, limits=BundleLimits(),
                                detail={"baseline_bundle": baseline, "baseline_identity": {
                                    "connection_id": "notes", "skill_id": "remote-note", "version": 3,
                                }},
                            )
                            candidate_id = item["candidate_id"]
                            await candidates.revise_content(
                                conn, tenant_id="alpha", candidate_id=candidate_id, bundle=bundle,
                                limits=BundleLimits(), actor="admin",
                            )
                        await lib.commit_bundle(
                            conn, tenant_id="alpha", name="note-craft", bundle=bundle, limits=BundleLimits(),
                            mutation_id=f"change-{index}", actor="admin", source=source,
                            metadata={"candidate_id": candidate_id, "revision": 2, "user_modified": True}
                            if source == "candidate-review" else {},
                        )
                        task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                        operation = await repo.operation(conn, task.task_id)
                        connection = await repo.connection(conn, "notes")
                        if source == "candidate-review":
                            await candidates.transition(
                                conn, tenant_id="alpha", candidate_id=candidate_id, to_status="validating",
                                actor="admin", expected_revision=2, validate_task_id=task.task_id,
                            )
                async with remote.client() as client:
                    await write_skill(task, services, client, connection, operation)
            async with skill_scope(services, "alpha", versions={"note-craft": [3, 4]}):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    history = await lib.list_versions(conn, "note-craft")
                    assert [v["provenance"]["kind"] for v in history] == ["te", "manual", "unknown", "unknown"]
                    te = await lib.get_version(conn, "note-craft", 4)
                    assert te["provenance"]["base_version"] == 3
                    assert te["provenance"]["candidate_id"] == candidate_id
                    assert te["provenance"]["user_modified"]
                    assert te["actor"] == "admin"
                    context = await version_context(conn, pg_env.tx, "note-craft", 4, provenance=te["provenance"])
                    assert context["baseline"]["version"] == 3
                    assert context["baseline"]["connection_id"] == "notes"
                    assert not await repo.skill_version_receipts(conn, "other", "remote-note")
                    assert not await repo.skill_version_receipts(conn, "notes", "different-native-id")
            async with pg_env.tx.tenant_tx("beta") as conn:
                assert not await repo.skill_version_receipts(conn, "notes", "remote-note")
            assert remote.posts.count("skills/remote-note/versions") == 2
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_same_name_cloud_skills_require_connection_selection(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            async with pg_env.tx.tenant_tx("alpha") as conn:
                connection = await repo.connection(conn, "notes")
                await repo.save(conn, tenant_id="alpha", connection_id="other",
                                config={**connection["config"], "entry_agent_id": "another-root"})
                await repo.bind(conn, tenant_id="alpha", connection_id="other", kind="skill", name="note-craft",
                                record={"id": "remote-note", "name": "note-craft", "agents": []})
            library = SkillLibraryRepository(pg_env.tx)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    heads = await library.list_skills(conn)
                    assert {h["connection_id"] for h in heads} == {"notes", "other"}
                    from team_skills.library.repository import SkillMutationConflict
                    with pytest.raises(SkillMutationConflict, match="connection_id"):
                        await library.read_current_bundle(conn, "note-craft")
            async with skill_scope(services, "alpha", connection_id="other"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    head, _ = await library.read_current_bundle(conn, "note-craft")
                    assert head["connection_id"] == "other"
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_failed_cloud_candidate_publication_returns_proposal_to_review(pg_env):
    async def run():
        from team_ma.handlers import _handle
        from team_skills.candidates.repository import CandidateRepository
        from team_skills.library.bundle import encode_bundle_payload
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            candidates = CandidateRepository(pg_env.tx)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    head, files = await SkillLibraryRepository(pg_env.tx).read_current_bundle(conn, "note-craft")
                    item = await candidates.create(
                        conn, tenant_id="alpha", skill_name="note-craft", bundle=files, limits=BundleLimits(),
                        base_version=2, detail={"baseline_bundle": encode_bundle_payload(files),
                        "baseline_identity": {k: head[k] for k in ("skill_id", "connection_id", "version")}},
                    )
                    commit = await SkillLibraryRepository(pg_env.tx).commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="candidate-write",
                        limits=BundleLimits(), base_version=2,
                        metadata={"candidate_id": item["candidate_id"], "revision": item["revision"]},
                    )
                    await candidates.transition(
                        conn, tenant_id="alpha", candidate_id=item["candidate_id"], to_status="validating",
                        actor="test", validate_task_id=commit.record["job"]["job_id"],
                    )
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
            remote.archives[3] = skill_zip(body="External edit after acceptance")
            remote.titles[3] = "note-craft"
            outcome = await _handle(task, services)
            assert outcome.outcome == "fail"
            async with pg_env.tx.tenant_tx("alpha") as conn:
                failed = await candidates.require(conn, item["candidate_id"])
                assert failed["status"] == "open" and not failed["published_version"]
                assert failed["decision"]["status"] == "cloud_publish_failed"
            assert remote.posts == []
        finally:
            reset_current_tenant(token)
    asyncio.run(run())


def test_cloud_edit_uploads_native_version_and_rejects_a_changed_base(pg_env):
    async def run():
        token = set_current_tenant(TenantContext("alpha"))
        try:
            remote = NativeSkills()
            services, repo, _ = await setup(pg_env, remote)
            library = SkillLibraryRepository(pg_env.tx)
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    head, files = await library.read_current_bundle(conn, "note-craft")
                    files["SKILL.md"] += b"\nTE reviewed update\n"
                    commit = await library.commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="edit",
                        limits=BundleLimits(), base_version=head["version"],
                    )
                    assert commit.status == "accepted"
                    assert await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skill_versions") == 0
                    task = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, task.task_id)
                    connection = await repo.connection(conn, "notes")
            async with remote.client() as client:
                result = await write_skill(task, services, client, connection, operation)
            assert result["version"] == 3 and result["readback_verified"]
            assert remote.agent["skills"][0]["version"] == "3"
            assert remote.agent["system"] == "External system remains"
            async with skill_scope(services, "alpha"):
                async with pg_env.tx.tenant_tx("alpha") as conn:
                    repeated = await library.commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="edit",
                        limits=BundleLimits(), base_version=2,
                    )
                    assert repeated.record["job"]["job_id"] == task.task_id
                    files["SKILL.md"] += b"\nNext update\n"
                    await library.commit_bundle(
                        conn, tenant_id="alpha", name="note-craft", bundle=files, mutation_id="stale",
                        limits=BundleLimits(), base_version=3,
                    )
                    stale = await services.tasks.claim(conn, owner_id="test", lease_seconds=300, kinds=["ma.skill"])
                    operation = await repo.operation(conn, stale.task_id)
            remote.archives[4] = skill_zip(body="Concurrent external edit")
            remote.titles[4] = "note-craft"
            count = len(remote.posts)
            async with remote.client() as client:
                with pytest.raises(ValueError, match="changed after acceptance"):
                    await write_skill(stale, services, client, connection, operation)
            assert len(remote.posts) == count and max(remote.archives) == 4
        finally:
            reset_current_tenant(token)
    asyncio.run(run())
