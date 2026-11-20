import pytest

from team_ma.repository import MARepository
from team_ma.schemas import MAConflict, validate_config
from teamEvolver.security import SecretBox, SecretRepository

pytestmark = pytest.mark.postgres


def test_connections_revisions_effects_and_rls(pg_env):
    repo = MARepository(pg_env.tx)
    secrets = SecretRepository(pg_env.tx, SecretBox("test-master"))

    async def create(conn):
        ma = await secrets.store(conn, tenant_id="alpha", plaintext="ark-key")
        token = await secrets.store(conn, tenant_id="alpha", plaintext="tevt_testing")
        config = validate_config({
            "display_name": "Generic MA", "entry_agent_id": "agent-root", "environment_id": "env",
            "default_user_id": "operator", "ma_secret_ref": ma.ref, "tenant_token_ref": token.ref,
        })
        await repo.save(conn, tenant_id="alpha", connection_id="first", config=config)
        row = await repo.connection(conn, "first")
        assert row["revision"] == 1
        assert "ark-key" not in str(row)
        await repo.save(conn, tenant_id="alpha", connection_id="first",
                        config={**config, "display_name": "renamed"}, expected_revision=1)
        assert (await repo.connection(conn, "first", 1))["config"]["display_name"] == "Generic MA"
        with pytest.raises(MAConflict):
            await repo.save(conn, tenant_id="alpha", connection_id="first", config=config, expected_revision=1)
        await repo.prepare_effect(conn, tenant_id="alpha", connection_id="first", effect_id="effect",
                                  owner_id="run", kind="replay-session", intent={"title": "owned"})
        await repo.confirm_effect(conn, "effect", "sesn-test", {"id": "sesn-test"})
        assert await repo.replay_session_ids(conn, "first") == {"sesn-test"}
        await repo.status(conn, "first", "error", {"error": "transport", "job_id": "failed", "agent_count": 2})
        before = await repo.connection(conn, "first")
        await repo.status(conn, "first", "ready", before["detail"], synced=True)
        assert (await repo.connection(conn, "first"))["detail"] == {"agent_count": 2}
        return config

    config = pg_env.tx.run_tenant_tx("alpha", create)

    async def cross_tenant(conn):
        assert await repo.connection(conn, "first") is None
        assert await repo.effect(conn, "effect") is None
        assert await repo.connections(conn) == []
        # Global unique target occupancy remains effective across tenant RLS.
        with pytest.raises(MAConflict):
            await repo.save(conn, tenant_id="beta", connection_id="second", config=config)
    pg_env.tx.run_tenant_tx("beta", cross_tenant)


@pytest.mark.parametrize("changed, reusable", [
    ({"poll_interval_seconds": 86400, "display_name": "Renamed"}, True),
    ({"sync_interval_seconds": 120}, True),
    ({"replay_environment_id": "different-isolation"}, False),
    ({"environment_id": "different-environment"}, False),
    ({"ma_secret_ref": "new-credential"}, False),
    ({"runtime_binding": {"source": "changed"}}, False),
])
def test_discovery_survives_only_non_execution_config_changes(pg_env, changed, reusable):
    repo = MARepository(pg_env.tx)

    async def run(conn):
        config = validate_config({
            "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
            "default_user_id": "operator", "ma_secret_ref": "ark", "tenant_token_ref": "pull",
            "replay_enabled": True, "replay_environment_id": "isolation",
        })
        await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
        await repo.accept(conn, tenant_id="alpha", connection_id="one", operation_id="discover",
                          kind="ma.discover", payload={"revision": 1})
        proof = {"replay_environment": {"id": "isolation", "config": {"type": "cloud"}}}
        await repo.checkpoint(conn, "discover", proof, {"status": "ready"})
        await repo.save(conn, tenant_id="alpha", connection_id="one",
                        config={**config, **changed}, expected_revision=1)
        assert await repo.discovery_snapshot(conn, "one", 2) == (proof if reusable else None)
    pg_env.tx.run_tenant_tx("alpha", run)


def test_retired_effect_history_cannot_hide_pending_cleanup(pg_env):
    repo = MARepository(pg_env.tx)

    async def run(conn):
        config = validate_config({
            "display_name": "Notes", "entry_agent_id": "root", "environment_id": "env",
            "default_user_id": "operator", "ma_secret_ref": "ark", "tenant_token_ref": "pull",
        })
        await repo.save(conn, tenant_id="alpha", connection_id="one", config=config)
        await conn.execute(
            f"INSERT INTO {repo.schema}.ma_external_effects "
            "(tenant_id,connection_id,effect_id,owner_id,kind,request_hash,intent,status,created_at) "
            "SELECT 'alpha','one','old-'||i,'run','replay-session','hash','{}'::jsonb,'cleaned',"
            "now()-interval '1 day' FROM generate_series(1,1001) AS i")
        await repo.prepare_effect(conn, tenant_id="alpha", connection_id="one", effect_id="fresh",
                                  owner_id="run", kind="replay-session", intent={})
        await repo.confirm_effect(conn, "fresh", "fresh-session", {"id": "fresh-session"})
        assert "fresh" in {e["effect_id"] for e in await repo.effects(conn, "one", owner_id="run")}
    pg_env.tx.run_tenant_tx("alpha", run)
