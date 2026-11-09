"""Real PostgreSQL: execution Candidates share the audited lifecycle, never Skill rows."""
from __future__ import annotations

import asyncio

import pytest

from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.history import candidate_payload
from team_skills.execution.evolution import attach_execution_proposal, planning_context
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def register(env, profile_id="customer"):
    async def setup(conn):
        raw = profile_raw(profile_id, prefix=profile_id + "-")
        raw["affected_skill_ids"] = []
        return await ExecutionRepository(env.tx).register(
            conn, tenant_id="alpha", raw=raw, skill_dependencies=[], actor="test",
        )
    return _run(env, "alpha", setup)


def proposal(profile_id, release, content="Improved business Prompt"):
    context = {"candidate_kind": "execution", "execution_profile_id": profile_id,
               "base_release_id": release["release_id"],
               "baseline_execution_plan": release["document"]["execution_plan"],
               "affected_skill_ids": [], "skill_dependencies": []}
    return attach_execution_proposal(
        {"component_changes": [component_change(content, component_id=profile_id + "-main-prompt")]}, {},
        context=context, allowed_modules=["prompt"], working_bundle={}, skill_changed=False,
    )["_execution_detail"]


def create(env, profile_id, detail):
    return _run(env, "alpha", lambda conn: CandidateRepository(env.tx).create(
        conn, tenant_id="alpha", skill_name=None, bundle={}, limits=BundleLimits(), detail=detail,
    ))


def test_concurrent_prompt_candidates_converge_per_profile_and_rls_keeps_them_private(pg_env):
    _, release = register(pg_env)
    detail = proposal("customer", release)

    async def compete():
        async def one():
            async with pg_env.tx.tenant_tx("alpha") as conn:
                return await CandidateRepository(pg_env.tx).create(
                    conn, tenant_id="alpha", skill_name=None, bundle={}, limits=BundleLimits(), detail=detail,
                )
        return await asyncio.gather(one(), one())

    first, second = pg_env.tx.pool.run(compete())
    assert first["candidate_id"] == second["candidate_id"]
    assert {first["revision"], second["revision"]} == {1, 2}
    assert first["candidate_kind"] == "execution" and first["skill_name"] is None
    assert _run(pg_env, "alpha", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skills")) == 0
    assert _run(pg_env, "alpha", lambda c: c.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.skill_candidate_files",
    )) == 0
    assert _run(pg_env, "beta", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skill_candidates")) == 0
    _, other = register(pg_env, "another-customer")
    independent = create(pg_env, "another-customer", proposal("another-customer", other))
    assert independent["candidate_id"] != first["candidate_id"]


def test_execution_candidate_cannot_gain_skill_files_or_switch_frozen_profile(pg_env):
    _, release = register(pg_env)
    detail = proposal("customer", release)
    row = create(pg_env, "customer", detail)
    repo = CandidateRepository(pg_env.tx)
    for changed in ({**detail, "execution_profile_id": "other"}, {**detail, "skill_changed": True}):
        with pytest.raises(SkillLibraryError):
            _run(pg_env, "alpha", lambda c: repo.revise_detail(
                c, tenant_id="alpha", candidate_id=row["candidate_id"], detail=changed,
                actor="reviewer", expected_revision=1,
            ))
    with pytest.raises(SkillLibraryError, match="no Skill files"):
        _run(pg_env, "alpha", lambda c: repo.revise_content(
            c, tenant_id="alpha", candidate_id=row["candidate_id"], bundle={"SKILL.md": b"fake"},
            limits=BundleLimits(), actor="reviewer", expected_revision=1,
        ))
    assert _run(pg_env, "alpha", lambda c: repo.require(c, row["candidate_id"]))["revision"] == 1


def test_prompt_candidate_payload_and_profile_planning_are_truthful(pg_env):
    _, release = register(pg_env)
    detail = proposal("customer", release)
    row = create(pg_env, "customer", detail)
    value = _run(pg_env, "alpha", lambda c: candidate_payload(c, pg_env.tx, row))
    assert value["candidate_kind"] == "execution" and value["execution_profile_id"] == "customer"
    assert value["skill_name"] is value["candidate_skill_name"] is value["current_skill"] is None
    assert value["candidate_skill"] is None and value["candidate_skill_md"] == value["current_skill_md"] == ""
    assert value["bundle_diff"]["changed_count"] == 0
    context, gap = _run(pg_env, "alpha", lambda c: planning_context(
        c, pg_env.tx, profile_id="customer", skill_name=None, allowed_modules=["prompt"],
    ))
    assert not gap and context["candidate_kind"] == "execution"
    assert context["affected_skill_ids"] == context["skill_dependencies"] == []
    _, gap = _run(pg_env, "alpha", lambda c: planning_context(
        c, pg_env.tx, profile_id="customer", skill_name="real-skill", allowed_modules=["prompt"],
    ))
    assert gap == "skill_not_bound_to_execution_profile"
