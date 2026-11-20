"""Compact candidate reads bound database traffic before Python decoding."""
import json

import pytest

from team_skills.candidates.repository import CandidateRepository
from tests.support.candidates import seed_candidate
from tests.support.postgres import _run


@pytest.mark.postgres
def test_compact_list_keeps_metadata_without_loading_evidence(pg_env):
    candidate_id = seed_candidate(pg_env)
    detail = {
        "rationale": "Preserve review metadata",
        "static_validation": {"ok": True},
        "session_evidence": {"transcript": "x" * 1_000_000},
        "replay_cases": [{"messages": "y" * 1_000_000}],
        "current_skill": {"bundle": "z" * 1_000_000},
    }

    async def check(conn):
        await conn.execute(
            f"UPDATE {pg_env.schema}.skill_candidates SET detail=$2::jsonb WHERE candidate_id=$1",
            candidate_id, json.dumps(detail),
        )
        repo = CandidateRepository(pg_env.tx)
        rows, total = await repo.list(conn, compact=True)
        assert total == 1
        assert rows[0]["detail"] == {
            "rationale": detail["rationale"], "static_validation": detail["static_validation"],
        }
        assert len(json.dumps(rows[0], default=str)) < 2_000
        assert (await repo.require(conn, candidate_id))["detail"] == detail
        assert (await repo.list(conn))[0][0]["detail"] == detail

    _run(pg_env, "default", check)
