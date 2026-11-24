"""Merge writes and source archival commit together under revision guards."""
import os

import pytest

from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits
from team_skills.library.repository import SkillNotFound
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="PostgreSQL required")


def setup(env):
    async def seed(conn):
        repo = CandidateRepository(env.tx)
        return [await repo.create(
            conn, tenant_id="alpha", skill_name=name, bundle={"SKILL.md": _skill_md(name), "ref.txt": name.encode()},
            limits=BundleLimits(), proposed_action="create",
        ) for name in ("first", "second")]
    return _run(env, "alpha", seed)


def inputs(items):
    return [{key: item[key] for key in ("candidate_id", "skill_name", "revision", "base_version", "tree_sha256")}
            for item in items]


@pytest.mark.postgres
def test_merge_archives_sources_and_clears_target_verdict_in_one_transaction(pg_env):
    items = setup(pg_env)
    repo = CandidateRepository(pg_env.tx)

    async def merge(conn):
        await repo.lock_merge_inputs(conn, tenant_id="alpha", inputs=inputs(items))
        target = await repo.create(
            conn, tenant_id="alpha", skill_name="first",
            bundle={"SKILL.md": _skill_md("first"), "ref.txt": b"merged"},
            limits=BundleLimits(), proposed_action="create",
        )
        await repo.finish_merge(conn, tenant_id="alpha", target=target, inputs=inputs(items))
        return target, await repo.require(conn, items[1]["candidate_id"])

    target, source = _run(pg_env, "alpha", merge)
    assert target["candidate_id"] == items[0]["candidate_id"] and target["revision"] == 2
    assert target["verdict"] == "" and source["status"] == "archived"
    assert source["decision"]["merged_into"] == target["candidate_id"]
    archived_bundle = _run(pg_env, "alpha", lambda c: repo.read_bundle(c, source["candidate_id"]))
    assert archived_bundle["ref.txt"] == b"second"
    events = _run(pg_env, "alpha", lambda c: repo.events(c, target["candidate_id"]))
    assert events[-1]["detail"]["consolidation"]["inputs"] == inputs(items)
    with pytest.raises(SkillNotFound):
        _run(pg_env, "beta", lambda c: repo.lock_merge_inputs(c, tenant_id="beta", inputs=inputs(items)))


@pytest.mark.postgres
@pytest.mark.parametrize("mutation", ["edit", "validating", "archive"])
def test_stale_merge_leaves_target_and_sources_unchanged(pg_env, mutation):
    items = setup(pg_env)
    repo = CandidateRepository(pg_env.tx)

    async def change(conn):
        if mutation == "edit":
            await repo.revise_content(
                conn, tenant_id="alpha", candidate_id=items[1]["candidate_id"],
                bundle={"SKILL.md": _skill_md("second", body="Human edit")},
                limits=BundleLimits(), actor="human",
            )
        else:
            await repo.transition(
                conn, tenant_id="alpha", candidate_id=items[1]["candidate_id"], actor="human",
                to_status="validating" if mutation == "validating" else "archived",
            )
    _run(pg_env, "alpha", change)
    with pytest.raises(CandidateStateError, match="changed"):
        _run(pg_env, "alpha", lambda c: repo.lock_merge_inputs(c, tenant_id="alpha", inputs=inputs(items)))
    target = _run(pg_env, "alpha", lambda c: repo.require(c, items[0]["candidate_id"]))
    assert target["revision"] == 1 and target["status"] == "open"


@pytest.mark.postgres
def test_failure_after_target_revision_rolls_back_entire_merge(pg_env):
    items = setup(pg_env)
    repo = CandidateRepository(pg_env.tx)

    async def failed(conn):
        await repo.lock_merge_inputs(conn, tenant_id="alpha", inputs=inputs(items))
        target = await repo.create(
            conn, tenant_id="alpha", skill_name="first", bundle={"SKILL.md": _skill_md("first")},
            limits=BundleLimits(), proposed_action="create",
        )
        await repo.finish_merge(conn, tenant_id="alpha", target=target, inputs=inputs(items))
        raise RuntimeError("downstream failure")
    with pytest.raises(RuntimeError):
        _run(pg_env, "alpha", failed)
    for item in items:
        actual = _run(pg_env, "alpha", lambda c: repo.require(c, item["candidate_id"]))
        assert actual["revision"] == 1 and actual["status"] == "open"
