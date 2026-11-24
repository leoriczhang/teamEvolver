"""Runtime changes retain pinned Cases; disabled sources remain unavailable."""
import copy
import os

import pytest

from team_replay.datasets.candidate_selection import freeze_candidate_cases
from team_replay.datasets.repository import DatasetRepository
from team_replay.datasets.schema import make_document
from team_replay.fingerprints import execution_fingerprint
from tests.support.postgres import _run
from tests.support.replay import document

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL required",
)]


@pytest.mark.parametrize("change", ["environment", "model", "skills"])
def test_runtime_change_retains_case_and_historical_snapshot(pg_env, change):
    runtime = {
        "type": "managed-agents", "connection_id": "ma",
        "agent_graph": {"agents": {"agent": {"id": "agent", "model": {"id": "old"}, "skills": []}}},
        "environment": {"config": {"type": "cloud", "setup_script": "old"}},
    }
    current = copy.deepcopy(runtime)
    if change == "environment":
        current["environment"]["config"]["setup_script"] = "new"
    elif change == "model":
        current["agent_graph"]["agents"]["agent"]["model"]["id"] = "new"
    else:
        current["agent_graph"]["agents"]["agent"]["skills"] = [{"id": "another", "version": 2}]
    case = copy.deepcopy(document()["cases"][0])
    case.update(skill_ids=["demo"])
    case["provenance"]["source_runtime"] = runtime

    async def check(conn):
        repo = DatasetRepository(pg_env.tx)
        saved = await repo.save(conn, tenant_id="default", document=make_document(
            dataset_id="historical", name="historical", skills=["demo"], cases=[case],
            source={"kind": "evolution"},
        ))
        frozen = await freeze_candidate_cases(
            conn, pg_env.tx, tenant_id="default", candidate_id="candidate", skill_name="demo",
            detail={"replay_cases": [case]}, seed="command", active_ma_connections={"ma"},
            execution_fingerprints={"ma": execution_fingerprint(current)},
        )
        assert len(frozen["replay_cases"]) == 1
        assert frozen["replay_cases"][0]["provenance"]["source_runtime"] == runtime
        archives = await repo.archives(conn, "historical", 1)
        assert archives == []
        manual = await repo.freeze(conn, "historical")
        assert manual["document_hash"] == saved["document_hash"]
        assert manual["execution_document"]["cases"][0]["provenance"]["source_runtime"] == runtime
        assert len(await repo.historical_cases(
            conn, skill_id="demo", before_dataset_id=frozen["replay_sampling"]["current_dataset_id"],
        )) == 1
    _run(pg_env, "default", check)
