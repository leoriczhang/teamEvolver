"""Real PostgreSQL Skill versions, candidate state, RLS and API replica consistency.

Each test uses a fresh schema with all current schema versions. Runtime cache
failure cannot invalidate committed versions; mutation IDs preserve idempotency."""

from __future__ import annotations

import asyncio
import base64
import os

import pytest

from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits
from team_skills.library.cache import SkillRuntimeCache
from team_skills.library.repository import (
    SkillBundleTooLarge,
    SkillLibraryError,
    SkillLibraryRepository,
    SkillMutationConflict,
    SkillNotFound,
)
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run
from tests.support.skills import _skill_md

DSN = os.environ.get("TEAMEVOLVER_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEAMEVOLVER_TEST_PG_DSN is not configured")

LIMITS = BundleLimits()


# --------------------------------------------------------------------------- #
# fixtures: one fresh schema per test, migrated by the real SchemaRunner
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# repository invariants
# --------------------------------------------------------------------------- #


@pytest.mark.postgres
def test_publish_update_rollback_delete_is_append_only(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def body(conn):
        v1 = await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo")},
            mutation_id="m1",
            limits=LIMITS,
        )
        v2 = await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo", "v2"), "scripts/run.sh": b"echo hi\n"},
            mutation_id="m2",
            limits=LIMITS,
        )
        same = await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo", "v2"), "scripts/run.sh": b"echo hi\n"},
            mutation_id="m3",
            limits=LIMITS,
        )
        rb = await lib.commit_rollback(conn, tenant_id="alpha", name="demo", target_version=1, mutation_id="m4")
        rolled = await lib.read_version_bundle(conn, "demo", rb.version)
        gone = await lib.commit_delete(conn, tenant_id="alpha", name="demo", mutation_id="m5")
        live = await lib.list_skills(conn)
        history = await lib.list_versions(conn, "demo")
        v2_bundle = await lib.read_version_bundle(conn, "demo", 2)
        return v1, v2, same, rb, rolled, gone, live, history, v2_bundle

    v1, v2, same, rb, rolled, gone, live, history, v2_bundle = _run(pg_env, "alpha", body)
    assert (v1.action, v1.version) == ("publish", 1)
    assert (v2.action, v2.version) == ("update", 2)
    assert (same.status, same.version) == ("unchanged", 2)  # identical tree: no new version
    assert (rb.action, rb.version, rb.restored_from) == ("rollback", 3, 1)
    assert set(rolled) == {"SKILL.md"}
    assert (gone.action, gone.version) == ("delete", 4)  # tombstone is a version too
    assert live == []  # deleted skill leaves the manifest
    assert [h["version"] for h in history] == [4, 3, 2, 1]  # nothing rewritten
    assert v2_bundle["scripts/run.sh"] == b"echo hi\n"  # old versions stay readable


@pytest.mark.postgres
def test_repeated_mutation_id_replays_and_reuse_for_other_request_conflicts(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def body(conn):
        first = await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo")},
            mutation_id="same-id",
            limits=LIMITS,
        )
        again = await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo")},
            mutation_id="same-id",
            limits=LIMITS,
        )
        versions = await lib.list_versions(conn, "demo")
        return first, again, versions

    first, again, versions = _run(pg_env, "alpha", body)
    assert first.version == again.version == 1 and again.replayed
    assert len(versions) == 1

    async def reuse(conn):
        await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="demo",
            bundle={"SKILL.md": _skill_md("demo", "x")},
            mutation_id="same-id",
            limits=LIMITS,
        )

    with pytest.raises(SkillMutationConflict):
        _run(pg_env, "alpha", reuse)


@pytest.mark.postgres
def test_concurrent_mutations_of_one_skill_serialise_into_distinct_versions(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def publish(i: int):
        async with pg_env.tx.tenant_tx("alpha") as conn:
            return await lib.commit_bundle(
                conn,
                tenant_id="alpha",
                name="race",
                bundle={"SKILL.md": _skill_md("race", f"body {i}")},
                mutation_id=f"race-{i}",
                limits=LIMITS,
            )

    async def main():
        results = await asyncio.gather(*(publish(i) for i in range(8)))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            versions = await lib.list_versions(conn, "race")
            head = await lib.get_skill(conn, "race")
        await pg_env.tx.pool.aclose()
        return results, versions, head

    results, versions, head = asyncio.run(main())
    assert sorted(r.version for r in results) == list(range(1, 9))  # no lost / duplicate version
    assert [v["version"] for v in versions] == list(range(8, 0, -1))
    assert head["version"] == 8


@pytest.mark.postgres
def test_same_mutation_id_raced_concurrently_publishes_once(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def publish():
        async with pg_env.tx.tenant_tx("alpha") as conn:
            return await lib.commit_bundle(
                conn,
                tenant_id="alpha",
                name="once",
                bundle={"SKILL.md": _skill_md("once")},
                mutation_id="candidate:rev1",
                limits=LIMITS,
            )

    async def main():
        results = await asyncio.gather(*(publish() for _ in range(6)))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            versions = await lib.list_versions(conn, "once")
        await pg_env.tx.pool.aclose()
        return results, versions

    results, versions = asyncio.run(main())
    assert {r.version for r in results} == {1}
    assert sum(not r.replayed for r in results) == 1
    assert len(versions) == 1


@pytest.mark.postgres
def test_bundle_is_validated_before_any_write(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)
    tight = BundleLimits(max_files=2, max_bytes=10_000, max_file_bytes=5_000)
    bad = [
        ({"README.md": b"no entrypoint"}, SkillLibraryError),
        ({"SKILL.md": b"# no frontmatter\n"}, SkillLibraryError),
        ({"SKILL.md": _skill_md("other-name")}, SkillLibraryError),  # name mismatch
        ({"SKILL.md": b"---\nname: demo\ndescription: x\n---\n---\nname: demo\n---\nbody"}, SkillLibraryError),
        ({"SKILL.md": _skill_md("demo"), "../escape.sh": b"x"}, SkillLibraryError),
        ({"SKILL.md": _skill_md("demo"), "a": b"1", "b": b"2"}, SkillBundleTooLarge),
        ({"SKILL.md": _skill_md("demo"), "big.bin": b"x" * 6_000}, SkillBundleTooLarge),
    ]
    for i, (bundle, error) in enumerate(bad):

        async def body(conn, bundle=bundle, i=i):
            await lib.commit_bundle(
                conn, tenant_id="alpha", name="demo", bundle=bundle, mutation_id=f"bad-{i}", limits=tight
            )

        with pytest.raises(error):
            _run(pg_env, "alpha", body)

    async def check(conn):
        return await lib.get_skill(conn, "demo", include_deleted=True)

    assert _run(pg_env, "alpha", check) is None  # nothing was committed


@pytest.mark.postgres
def test_rls_isolates_tenants_on_every_skill_table(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def publish(conn):
        await lib.commit_bundle(
            conn,
            tenant_id="alpha",
            name="secret",
            bundle={"SKILL.md": _skill_md("secret")},
            mutation_id="m1",
            limits=LIMITS,
        )

    _run(pg_env, "alpha", publish)

    async def peek(conn):
        counts = {}
        for table in ("skills", "skill_versions", "skill_version_files", "skill_blobs", "skill_mutations"):
            counts[table] = await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.{table}")
        return counts, await lib.get_skill(conn, "secret")

    counts, head = _run(pg_env, "beta", peek)
    assert all(n == 0 for n in counts.values()), counts
    assert head is None

    async def forge(conn):  # WITH CHECK rejects writing another tenant's rows
        await conn.execute(
            f"INSERT INTO {pg_env.schema}.skills (tenant_id, name, skill_id) VALUES ('alpha', 'forged', 'x')"
        )

    with pytest.raises(Exception, match="row-level security"):
        _run(pg_env, "beta", forge)


@pytest.mark.postgres
def test_candidate_state_machine_is_audited_and_guards_revisions(pg_env):
    cands = CandidateRepository(pg_env.tx)

    async def body(conn):
        c = await cands.create(
            conn, tenant_id="alpha", skill_name="demo", bundle={"SKILL.md": _skill_md("demo")}, limits=LIMITS
        )
        # A second evolution cycle revises the live candidate in place.
        again = await cands.create(
            conn, tenant_id="alpha", skill_name="demo", bundle={"SKILL.md": _skill_md("demo", "better")}, limits=LIMITS
        )
        await cands.transition(
            conn,
            tenant_id="alpha",
            candidate_id=c["candidate_id"],
            to_status="validating",
            actor="worker",
            validate_task_id="t1",
        )
        stale = await cands.record_verdict(
            conn, tenant_id="alpha", candidate_id=c["candidate_id"], revision=1, verdict="accept", evaluation={}
        )
        fresh = await cands.record_verdict(
            conn,
            tenant_id="alpha",
            candidate_id=c["candidate_id"],
            revision=2,
            verdict="inconclusive",
            evaluation={"replay": {}},
        )
        marked = await cands.set_marks(
            conn, tenant_id="alpha", candidate_id=c["candidate_id"], changes={"reviewed": True}, actor="alice"
        )
        done = await cands.transition(
            conn,
            tenant_id="alpha",
            candidate_id=c["candidate_id"],
            to_status="published",
            actor="alice",
            expected_revision=2,
            published_version=1,
            mutation_id="candidate:alpha:demo:r2",
        )
        events = await cands.events(conn, c["candidate_id"])
        bundle = await cands.read_bundle(conn, c["candidate_id"])
        return c, again, stale, fresh, marked, done, events, bundle

    c, again, stale, fresh, marked, done, events, bundle = _run(pg_env, "alpha", body)
    assert again["candidate_id"] == c["candidate_id"] and again["revision"] == 2
    assert stale is None  # a late verdict for an old revision is dropped
    assert fresh["verdict"] == "inconclusive" and fresh["status"] == "open"
    assert marked["reviewed"] is True
    assert done["status"] == "published" and done["adopted"] is True and done["decided_at"] is not None
    assert [e["kind"] for e in events] == ["created", "revised", "transition", "verdict", "feedback", "transition"]
    assert b"better" in bundle["SKILL.md"]

    async def illegal(conn):
        await cands.transition(conn, tenant_id="alpha", candidate_id=c["candidate_id"], to_status="open", actor="alice")

    with pytest.raises(CandidateStateError):  # published is terminal
        _run(pg_env, "alpha", illegal)

    async def stale_publish(conn):
        n = await cands.create(
            conn, tenant_id="alpha", skill_name="demo", bundle={"SKILL.md": _skill_md("demo", "next")}, limits=LIMITS
        )
        await cands.transition(
            conn,
            tenant_id="alpha",
            candidate_id=n["candidate_id"],
            to_status="published",
            actor="alice",
            expected_revision=99,
        )

    with pytest.raises(CandidateStateError):  # stale revision refused
        _run(pg_env, "alpha", stale_publish)


# --------------------------------------------------------------------------- #
# HTTP surface through the composed app: two "pods" over one database
# --------------------------------------------------------------------------- #


class _FailingCache:
    """A runtime cache whose every write fails (disk full / permission)."""

    def materialize(self, tenant_id, name, bundle):
        return SkillRuntimeCache("/dev/null/not-a-dir").materialize(tenant_id, name, bundle)

    def remove(self, tenant_id, name):
        return {"materialized": True, "reason": "OSError: read-only"}


@pytest.mark.postgres
def test_two_pods_read_identical_skills_versions_and_manifest(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a, _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)

        created = a.post(
            "/api/skills",
            headers=H,
            json={
                "name": "demo",
                "description": "Use when testing.",
                "category": "coding",
                "body": "Step 1",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["created"] is True and created.json()["version"] == 1
        files = b.post(
            "/api/skills/demo/files",
            headers=H,
            json={
                "files": [{"path": "scripts/run.sh", "content_b64": base64.b64encode(b"echo hi\n").decode()}],
            },
        )
        assert files.status_code == 200, files.text
        assert files.json()["version"] == 2 and files.json()["written"] == ["scripts/run.sh"]

        # Both pods see the same manifest, versions and file content — PG is the authority.
        for client in (a, b):
            listing = client.get("/api/skills", headers=H).json()
            assert listing["skill_backend"] == "postgres"
            assert [(s["name"], s["version"], s["file_count"]) for s in listing["skills"]] == [("demo", 2, 2)]
            versions = client.get("/api/skills/demo/versions", headers=H).json()
            assert versions["current_version"] == 2 and versions["versions"] == [2, 1]
            v2 = client.get("/api/skills/demo/versions/2", headers=H).json()
            assert {f["path"] for f in v2["files"]} == {"SKILL.md", "scripts/run.sh"} and v2["is_current"]
        assert a.get("/api/skills", headers=H).json() == b.get("/api/skills", headers=H).json()

        rolled = a.post("/api/skills/demo/rollback?target_version=1", headers=H)
        assert rolled.status_code == 200 and rolled.json()["new_version"] == 3
        assert b.get("/api/skills/demo", headers=H).json()["files"] == ["SKILL.md"]
        assert b.delete("/api/skills/demo", headers=H).status_code == 200
        assert a.get("/api/skills", headers=H).json()["skills"] == []
        assert a.get("/api/skills/demo", headers=H).status_code == 404


@pytest.mark.postgres
def test_idempotency_key_replays_across_pods(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a, _pod(pg_env, tmp_path, monkeypatch, pod="b") as b:
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)
        body = {"name": "demo", "description": "Use when testing.", "body": "x"}
        first = a.post("/api/skills", headers={**H, "Idempotency-Key": "k1"}, json=body)
        retry = b.post("/api/skills", headers={**H, "Idempotency-Key": "k1"}, json=body)
        assert first.json()["version"] == retry.json()["version"] == 1
        assert retry.json()["replayed"] is True
        assert a.get("/api/skills/demo/versions", headers=H).json()["versions"] == [1]


@pytest.mark.postgres
def test_runtime_cache_failure_never_affects_committed_state(pg_env, tmp_path, monkeypatch):
    with (
        _pod(pg_env, tmp_path, monkeypatch, pod="a", cache=_FailingCache()) as a,
        _pod(pg_env, tmp_path, monkeypatch, pod="b") as b,
    ):
        _login(a, bootstrap=True)
        _login(b, bootstrap=False)
        resp = a.post("/api/skills", headers=H, json={"name": "demo", "description": "Use when testing."})
        assert resp.status_code == 200, resp.text  # request succeeds despite the cache
        assert resp.json()["runtime_cache"]["materialized"] is False
        assert resp.json()["version"] == 1
        # The commit is visible from the other pod (PG authority, not the cache).
        assert [s["name"] for s in b.get("/api/skills", headers=H).json()["skills"]] == ["demo"]
        assert a.delete("/api/skills/demo", headers=H).status_code == 200

        # The healthy pod's cache is rebuildable and never read back as authority.
        b.post("/api/skills", headers=H, json={"name": "kept", "description": "Use when testing."})
        cache_dir = tmp_path / "cache-b" / "kept"
        assert (cache_dir / "SKILL.md").exists()
        import shutil

        shutil.rmtree(tmp_path / "cache-b")
        assert [s["name"] for s in b.get("/api/skills", headers=H).json()["skills"]] == ["kept"]
        assert b.get("/api/skills/kept", headers=H).status_code == 200


@pytest.mark.postgres
def test_mutations_require_tenant_admin_and_reject_bad_input(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        assert a.post("/api/skills", headers=H, json={"name": "../x", "description": "d"}).status_code == 400
        assert a.post("/api/skills", headers=H, json={"name": "demo", "description": ""}).status_code == 400
        a.post("/api/skills", headers=H, json={"name": "demo", "description": "Use when testing."})
        assert (
            a.post(
                "/api/skills/demo/files", headers=H, json={"files": [{"path": "SKILL.md", "content_b64": "eA=="}]}
            ).status_code
            == 400
        )
        assert (
            a.post(
                "/api/skills/demo/files", headers=H, json={"files": [{"path": "x.sh", "content_b64": "%%%"}]}
            ).status_code
            == 400
        )
        assert a.delete("/api/skills/demo/files/SKILL.md", headers=H).status_code == 400
        assert (
            a.post(
                "/api/skills", headers=H, json={"name": "demo", "description": "Use when testing.", "base_version": 7}
            ).status_code
            == 409
        )
        from fastapi.testclient import TestClient

        anon = TestClient(a.app, base_url="http://192.0.2.10:1080")
        assert anon.post("/api/skills", headers=H, json={"name": "x", "description": "d"}).status_code == 401


@pytest.mark.postgres
def test_agent_pull_is_tenant_scoped_with_etag(pg_env, tmp_path, monkeypatch):
    lib = SkillLibraryRepository(pg_env.tx)

    async def seed(conn, tenant):
        await lib.commit_bundle(
            conn,
            tenant_id=tenant,
            name=f"skill-{tenant}",
            bundle={"SKILL.md": _skill_md(f"skill-{tenant}"), "ref.md": b"ref"},
            mutation_id=f"seed-{tenant}",
            limits=LIMITS,
        )

    for tenant in ("default", "alpha"):
        _run(pg_env, tenant, lambda conn, tenant=tenant: seed(conn, tenant))

    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        agent = {"Authorization": "Bearer tevt_alpha"}
        assert a.get("/sync/skills?user_id=alice").status_code == 401
        assert a.get("/sync/skills", headers=agent).status_code == 400  # user_id required
        first = a.get("/sync/skills?user_id=alice", headers=agent)
        assert first.status_code == 200, first.text
        skills = first.json()["skills"]
        assert [s["name"] for s in skills] == ["skill-alpha"]  # only its own tenant
        assert {f["path"] for f in skills[0]["files"]} == {"SKILL.md", "ref.md"}
        assert (
            a.get("/sync/skills?user_id=alice", headers={**agent, "If-None-Match": first.headers["ETag"]}).status_code
            == 304
        )
        assert a.get("/sync/skills?user_id=alice", headers={**agent, "X-Tenant-Id": "default"}).status_code == 403

        async def bump(conn):
            await lib.commit_bundle(
                conn,
                tenant_id="alpha",
                name="skill-alpha",
                bundle={"SKILL.md": _skill_md("skill-alpha", "v2")},
                mutation_id="bump",
                limits=LIMITS,
            )

        _run(pg_env, "alpha", bump)
        changed = a.get("/sync/skills?user_id=alice", headers={**agent, "If-None-Match": first.headers["ETag"]})
        assert changed.status_code == 200 and changed.headers["ETag"] != first.headers["ETag"]


@pytest.mark.postgres
def test_missing_skills_config_row_fails_closed(pg_env, tmp_path, monkeypatch):
    async def drop_row(conn):
        await conn.execute(f"DELETE FROM {pg_env.schema}.tenant_configs WHERE module = 'skills'")

    _run(pg_env, "default", drop_row)
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        resp = a.post("/api/skills", headers=H, json={"name": "demo", "description": "Use when testing."})
        assert resp.status_code == 503 and "skills" in resp.json()["detail"]
        lib = SkillLibraryRepository(pg_env.tx)
        assert _run(pg_env, "default", lambda conn: lib.get_skill(conn, "demo")) is None


@pytest.mark.postgres
def test_get_missing_skill_and_version_are_404(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as a:
        _login(a, bootstrap=True)
        assert a.get("/api/skills/nope", headers=H).status_code == 404
        assert a.get("/api/skills/nope/versions", headers=H).status_code == 404
        a.post("/api/skills", headers=H, json={"name": "demo", "description": "Use when testing."})
        assert a.get("/api/skills/demo/versions/9", headers=H).status_code == 404
        assert a.post("/api/skills/nope/rollback?target_version=1", headers=H).status_code == 404


@pytest.mark.postgres
def test_repository_not_found_paths(pg_env):
    lib = SkillLibraryRepository(pg_env.tx)

    async def body(conn):
        await lib.commit_delete(conn, tenant_id="alpha", name="ghost", mutation_id="d1")

    with pytest.raises(SkillNotFound):
        _run(pg_env, "alpha", body)
