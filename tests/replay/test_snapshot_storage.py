"""Full child-thread evidence survives repeated Dataset and command snapshots."""
import copy
import json

import pytest

from team_replay.datasets.candidate_selection import freeze_candidate_cases
from team_replay.datasets.repository import DatasetRepository
from team_replay.datasets.schema import make_document
from team_replay.repository import ReplayRepository
from team_skills.evolution.repository import EvolutionRepository, digest
from teamEvolver.storage.json_snapshots import SNAPSHOT_REFERENCE, JsonSnapshots
from teamEvolver.storage.postgres import PostgresObjectStore
from tests.support.postgres import _run
from tests.support.replay import document

pytestmark = pytest.mark.postgres


def cases():
    snapshot = {
        "session_id": "source",
        "turns": [{"agent_threads": [{"tool_result": "完整子线程证据" * 100_000}]}],
    }
    result = []
    for number in range(4):
        item = copy.deepcopy(document()["cases"][0])
        item.update(case_id=f"case-{number}", query=f"Task {number}", skill_ids=["demo"])
        item["provenance"] = {"session_snapshot": copy.deepcopy(snapshot)}
        result.append(item)
    return result


def test_repeated_dataset_and_command_snapshots_remain_complete_and_independent(pg_env, monkeypatch):
    monkeypatch.setattr("team_replay.datasets.repository.MAX_DATASET_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr("team_skills.evolution.repository.MAX_COMMAND_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr("team_replay.repository.MAX_REPLAY_INPUT_BYTES", 4 * 1024 * 1024)
    source = cases()

    async def exercise(conn):
        fixed = await freeze_candidate_cases(
            conn, pg_env.tx, tenant_id="alpha", candidate_id="candidate", skill_name="demo",
            detail={"replay_cases": source}, seed="fixed",
        )
        assert len(fixed["replay_cases"]) == 4
        for actual, original in zip(fixed["replay_cases"], source):
            assert actual["provenance"]["session_snapshot"] == original["provenance"]["session_snapshot"]
        repo = EvolutionRepository(pg_env.tx)
        command, _ = await repo.accept(
            conn, tenant_id="alpha", command_id="command", kind="skill.validate",
            request={}, payload={"detail": fixed},
        )
        assert command["payload"] == {"detail": fixed}
        assert command["payload_hash"] == digest({"detail": fixed})
        history, created = await ReplayRepository(pg_env.tx).accept(
            conn, tenant_id="alpha", domain="skill", run_id="command",
            subject_id="demo", request={}, payload={"job": command["payload"]},
        )
        assert created and history["input"] == {"job": command["payload"]}
        stored_history = await conn.fetchval(
            f"SELECT input::text FROM {pg_env.schema}.skill_replay_runs WHERE run_id='command'",
        )
        assert len(stored_history) < 10_000 and SNAPSHOT_REFERENCE in stored_history
        stored = await conn.fetchval(
            f"SELECT document::text FROM {pg_env.schema}.dataset_revisions LIMIT 1",
        )
        assert len(stored) < 10_000 and SNAPSHOT_REFERENCE in stored
        assert await conn.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.storage_objects "
            "WHERE starts_with(key,'session-snapshots/')",
        ) == 1
        hydrated = command["payload"]["detail"]["replay_cases"]
        hydrated[0]["provenance"]["session_snapshot"]["turns"].clear()
        assert hydrated[1]["provenance"]["session_snapshot"]["turns"]
        return json.loads(stored)["cases"][0]["provenance"]["session_snapshot"]

    reference = _run(pg_env, "alpha", exercise)
    with pytest.raises(FileNotFoundError):
        _run(pg_env, "beta", lambda conn: JsonSnapshots(pg_env.tx, "beta").hydrate(conn, reference))


def test_external_command_envelope_verifies_the_full_hydrated_payload(pg_env):
    payload = {"padding": "x" * (9 * 1024 * 1024), "detail": {"replay_cases": cases()}}
    repo = EvolutionRepository(pg_env.tx)

    async def exercise(conn):
        saved, _ = await repo.accept(
            conn, tenant_id="alpha", command_id="external", kind="skill.validate", request={}, payload=payload,
        )
        stored = await conn.fetchval(
            f"SELECT payload::text FROM {pg_env.schema}.skill_commands WHERE command_id='external'",
        )
        assert "$skill_command_payload" in stored
        assert saved["payload"] == payload and saved["payload_hash"] == digest(payload)

    _run(pg_env, "alpha", exercise)


@pytest.mark.parametrize("failure", ["missing", "changed", "forged"])
def test_dataset_refuses_missing_corrupt_or_forged_snapshot(pg_env, failure):
    repo = DatasetRepository(pg_env.tx)

    async def seed(conn):
        await repo.save(conn, tenant_id="alpha", document=make_document(
            dataset_id="dataset", name="Evidence", skills=["demo"], cases=cases(),
        ))
        stored = await conn.fetchval(
            f"SELECT document FROM {pg_env.schema}.dataset_revisions WHERE dataset_id='dataset'",
        )
        packed = json.loads(stored) if isinstance(stored, str) else stored
        reference = packed["cases"][0]["provenance"]["session_snapshot"][SNAPSHOT_REFERENCE]
        if failure == "missing":
            await conn.execute(
                f"DELETE FROM {pg_env.schema}.storage_objects WHERE key=$1", reference["key"],
            )
        elif failure == "changed":
            await PostgresObjectStore(pg_env.tx, "alpha").put(conn, reference["key"], b'{"changed":true}')
        else:
            reference["key"] = "unrelated/source.json"
            await conn.execute(
                f"UPDATE {pg_env.schema}.dataset_revisions SET document=$1::jsonb",
                json.dumps(packed),
            )

    _run(pg_env, "alpha", seed)
    error = FileNotFoundError if failure == "missing" else ValueError
    with pytest.raises(error):
        _run(pg_env, "alpha", lambda conn: repo.get(conn, "dataset"))


def test_dataset_and_snapshot_objects_roll_back_together(pg_env):
    repo = DatasetRepository(pg_env.tx)

    async def abort(conn):
        await repo.save(conn, tenant_id="alpha", document=make_document(
            dataset_id="dataset", name="Evidence", skills=["demo"], cases=cases(),
        ))
        raise RuntimeError("acceptance aborted")

    with pytest.raises(RuntimeError, match="acceptance aborted"):
        _run(pg_env, "alpha", abort)
    assert _run(pg_env, "alpha", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.storage_objects",
    )) == 0


def test_historical_sampling_hydrates_snapshot_references(pg_env):
    repo = DatasetRepository(pg_env.tx)
    source = cases()

    async def exercise(conn):
        await repo.save(conn, tenant_id="alpha", document=make_document(
            dataset_id="history", name="History", skills=["demo"], cases=source,
            source={"kind": "evolution"},
        ))
        recent = copy.deepcopy(source[0])
        recent.update(case_id="recent", query="New task")
        fixed = await freeze_candidate_cases(
            conn, pg_env.tx, tenant_id="alpha", candidate_id="candidate", skill_name="demo",
            detail={"replay_cases": [recent]}, seed="fixed",
        )
        assert fixed["replay_sampling"]["historical_selected"] == 3
        assert len(fixed["replay_cases"]) == 4
        assert all(
            case["provenance"]["session_snapshot"] == source[0]["provenance"]["session_snapshot"]
            for case in fixed["replay_cases"]
        )

    _run(pg_env, "alpha", exercise)
