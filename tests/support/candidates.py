"""Persisted candidate setup and lookup helpers."""

from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from tests.support.postgres import _run
from tests.support.skills import _skill_md


def seed_candidate(env, name="demo"):
    async def seed(conn):
        return await CandidateRepository(env.tx).create(
            conn,
            tenant_id="default",
            skill_name=name,
            bundle={"SKILL.md": _skill_md(name)},
            limits=BundleLimits(),
            proposed_action="create",
        )

    return _run(env, "default", seed)["candidate_id"]


def candidate(env, candidate_id):
    return _run(env, "default", lambda conn: CandidateRepository(env.tx).require(conn, candidate_id))
