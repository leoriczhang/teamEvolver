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

    def client(self, base="https://ma.example/api/v3", token="ark"):
        return MAClient(base, token, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        path = request.url.path.removeprefix("/api/v3/")
        if request.method == "POST" and path == "skills":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content,
            )
            archive = next(p.get_payload(decode=True) for p in message.iter_parts() if p.get_filename())
            item = read_skill_zip(archive)
            self.new = {"id": "remote-new", "name": item.name, "latest_version": "1"}
            self.new_archive = archive
            self.posts.append(path)
            return httpx.Response(200, json=self.new)
        if path == "skills/remote-new":
            return httpx.Response(200, json=self.new)
        if path == "skills/remote-new/versions/1":
            return httpx.Response(200, json={"skill_id": "remote-new", "version": "1"})
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
            count = len(remote.posts)
            async with remote.client() as client:
                with pytest.raises(ValueError, match="changed after acceptance"):
                    await write_skill(stale, services, client, connection, operation)
            assert len(remote.posts) == count and max(remote.archives) == 4
        finally:
            reset_current_tenant(token)
    asyncio.run(run())
