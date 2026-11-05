"""Large frozen Replay inputs remain complete, immutable and tenant bound."""
from types import SimpleNamespace

import pytest

from team_skills.evolution.candidate_pipeline import load_command
from team_skills.evolution.repository import EvolutionRepository, digest
from teamEvolver.storage.postgres import PostgresObjectStore
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def large_payload():
    return {
        "config_revision": 7,
        "detail": {
            "replay_cases": [
                {"case_id": "recent", "snapshot": "子线程完整证据" * 500_000},
                {"case_id": "historical", "checks": [{"id": "done", "text": "Complete"}]},
            ],
        },
    }


def test_large_command_round_trip_load_and_idempotency_preserve_all_evidence(pg_env):
    payload = large_payload()
    repo = EvolutionRepository(pg_env.tx)

    async def accept(conn):
        command, created = await repo.accept(
            conn, tenant_id="alpha", command_id="large", kind="skill.validate",
            request={"candidate_id": "candidate", "revision": 2}, payload=payload,
        )
        assert created and command["payload"] == payload
        assert command["payload_hash"] == digest(payload)
        stored = await conn.fetchrow(
            f"SELECT payload::text AS payload,payload_hash FROM {pg_env.schema}.skill_commands "
            "WHERE command_id='large'",
        )
        assert len(stored["payload"]) < 300
        assert "$skill_command_payload" in stored["payload"]
        same, created = await repo.accept(
            conn, tenant_id="alpha", command_id="large", kind="skill.validate",
            request={"candidate_id": "candidate", "revision": 2}, payload=payload,
        )
        assert not created and same["payload"] == payload
        return command

    command = _run(pg_env, "alpha", accept)
    assert _run(pg_env, "beta", lambda conn: repo.command(conn, "large")) is None

    async def load():
        return await load_command(
            SimpleNamespace(
                tenant_id="alpha", payload_ref="large", kind="skill.validate",
                payload_hash=command["payload_hash"],
            ),
            SimpleNamespace(tx=pg_env.tx),
        )

    assert pg_env.tx.pool.run(load())["payload"] == payload


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_large_command_refuses_missing_or_changed_snapshot(pg_env, failure):
    repo = EvolutionRepository(pg_env.tx)

    async def seed(conn):
        command, _ = await repo.accept(
            conn, tenant_id="alpha", command_id="large", kind="skill.validate",
            request={}, payload=large_payload(),
        )
        key = f"skill-commands/{command['payload_hash']}.json"
        if failure == "missing":
            await conn.execute(
                f"DELETE FROM {pg_env.schema}.storage_objects WHERE tenant_id='alpha' AND key=$1", key,
            )
        else:
            await PostgresObjectStore(pg_env.tx, "alpha").put(conn, key, b'{"changed":true}')

    _run(pg_env, "alpha", seed)
    expected = FileNotFoundError if failure == "missing" else ValueError
    with pytest.raises(expected):
        _run(pg_env, "alpha", lambda conn: repo.command(conn, "large"))


def test_large_command_and_snapshot_roll_back_together(pg_env):
    repo = EvolutionRepository(pg_env.tx)

    async def abort(conn):
        await repo.accept(
            conn, tenant_id="alpha", command_id="large", kind="skill.validate",
            request={}, payload=large_payload(),
        )
        raise RuntimeError("acceptance aborted")

    with pytest.raises(RuntimeError, match="acceptance aborted"):
        _run(pg_env, "alpha", abort)

    async def inspect(conn):
        assert await repo.command(conn, "large") is None
        assert await conn.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.storage_objects "
            "WHERE starts_with(key,'skill-commands/')",
        ) == 0

    _run(pg_env, "alpha", inspect)
