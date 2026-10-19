"""Tenant and user isolation, incremental snapshots and atomic Context events."""

import asyncio
import os

import pytest

from teamEvolver.context.repository import ContextRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.security.agent_principal import AgentPrincipal
from tests.support.postgres import _run


@pytest.mark.postgres
def test_context_append_duplicate_and_commit_are_atomic(pg_env):
    repo = ContextRepository(pg_env.tx)
    p = AgentPrincipal("default", "account", "alice")
    sid = _run(pg_env, "default", lambda c: repo.start_session(c, p, "external"))[0]["session_id"]
    calls = []

    async def append():
        async with pg_env.tx.tenant_tx("default") as conn:
            _, duplicate = await repo.event_status(conn, p, sid, "e1", 1, {"content": "a"})
            if not duplicate:
                calls.append("upstream")
                await asyncio.sleep(0.01)
                await repo.record_event(conn, p, sid, "e1", 1, {"content": "a"})
            return duplicate

    async def compete():
        return await asyncio.gather(append(), append())

    assert sorted(pg_env.tx.pool.run(compete())) == [False, True]
    assert calls == ["upstream"]
    _run(pg_env, "default", lambda c: repo.commit(c, p, sid, {"ok": True}))
    with pytest.raises(ValueError, match="committed"):
        _run(pg_env, "default", lambda c: repo.event_status(c, p, sid, "e2", 2, {"content": "b"}))
    row = _run(pg_env, "default", lambda c: repo.session(c, p, sid))
    assert row["last_sequence"] == 1 and row["committed"]
    assert _run(pg_env, "default", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.context_events")) == 1
    with pytest.raises(KeyError):
        _run(pg_env, "alpha", lambda c: repo.session(c, AgentPrincipal("alpha", "account", "alice"), sid))
    with pytest.raises(KeyError):
        _run(pg_env, "default", lambda c: repo.session(c, AgentPrincipal("default", "account", "bob"), sid))


@pytest.mark.postgres
def test_context_snapshots_incremental_reads_usage_and_revocation(pg_env):
    repo = ContextRepository(pg_env.tx)
    p = AgentPrincipal("default", "account", "alice")

    async def seed(conn):
        session, _ = await repo.start_session(conn, p, "external")
        sid = session["session_id"]
        rid, _ = await repo.issue_ref(
            conn, p, session_id=sid, scope="personal_memory", uri="viking://user/alice/memories/a.md", kind="memory"
        )
        await repo.save_snapshot(conn, p, sid, "snap", [{"context_ref": rid, "title": "a"}])
        await repo.record_read(conn, p, rid, "full", "fact")
        assert (await repo.usage_refs(conn, p, sid, [rid]))[0]["uri"].endswith("/a.md")
        snapshot = await repo.load_snapshot(conn, p, "snap")
        assert snapshot["items"][0]["expanded"]["full"] == "fact"
        assert await repo.load_snapshot(conn, AgentPrincipal("default", "account", "bob"), "snap") is None
        await repo.revoke(conn, p, rid)
        assert await repo.load_snapshot(conn, p, "snap") is None
        return rid

    rid = _run(pg_env, "default", seed)
    assert _run(pg_env, "alpha", lambda c: repo.ref(c, AgentPrincipal("alpha", "account", "alice"), rid)) is None


@pytest.mark.postgres
def test_context_usage_snapshot_is_fixed_expiring_and_user_bound(pg_env):
    from teamEvolver.context.usage import verify_usage

    repo = ContextRepository(pg_env.tx)
    p = AgentPrincipal("default", "account", "alice")

    async def seed(conn):
        rid, _ = await repo.issue_ref(
            conn, p, scope="personal_memory", uri="viking://user/alice/memories/a.md", kind="memory"
        )
        items = [{"context_ref": rid, "l0": "fixed"}]
        await repo.save_snapshot(conn, p, "", "snapshot", items)
        await repo.record_read(conn, p, rid, "full", "before")
        await repo.record_read(conn, p, rid, "full", "after")
        assert (await repo.load_snapshot(conn, p, "snapshot"))["items"][0]["expanded"]["full"] == "before"
        return rid

    rid = _run(pg_env, "default", seed)
    services = build_services(role="worker", tx=pg_env.tx)
    turns = [{"context_usage": {"context_snapshot_id": "snapshot", "memory_refs": [{"context_ref": rid}]}}]
    assert pg_env.tx.pool.run(verify_usage(services, p, turns))[0]["context_usage"]["verified"]
    with pytest.raises(ValueError, match="snapshot"):
        pg_env.tx.pool.run(verify_usage(services, AgentPrincipal("default", "account", "bob"), turns))
    with pytest.raises(ValueError, match="immutable"):
        _run(
            pg_env,
            "default",
            lambda c: repo.save_snapshot(c, p, "", "snapshot", [{"context_ref": rid, "l0": "changed"}]),
        )
    _run(
        pg_env,
        "default",
        lambda c: c.execute(f"UPDATE {pg_env.schema}.context_refs SET expires_at=now()-interval '1 second'"),
    )
    with pytest.raises(ValueError, match="snapshot"):
        pg_env.tx.pool.run(verify_usage(services, p, turns))


pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires PostgreSQL")
