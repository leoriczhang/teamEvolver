"""Real PG: independent execution versions, tenant RLS and concurrent publication."""

from __future__ import annotations

import asyncio
import copy
import os

import pytest

from team_skills.config import BundleLimits
from team_skills.evolution.repository import unpack
from team_skills.execution.contracts import apply_changes, composition_hash
from team_skills.execution.evolution import freeze_profile_scope, planning_context
from team_skills.execution.repository import ExecutionRepository, ExecutionVersionConflict
from team_skills.library.repository import SkillLibraryRepository, SkillNotFound
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured"),
]


def register(env, *, tenant="alpha", raw=None):
    async def save(conn):
        library = SkillLibraryRepository(env.tx)
        dependencies = []
        value = raw or profile_raw()
        for name in value["affected_skill_ids"]:
            await library.commit_bundle(
                conn, tenant_id=tenant, name=name, bundle={"SKILL.md": _skill_md(name)},
                mutation_id="seed:" + name, limits=BundleLimits(),
            )
            head = await library.get_skill(conn, name)
            dependencies.append({"name": name, "version": head["version"], "tree_sha256": head["tree_sha256"]})
        return await ExecutionRepository(env.tx).register(
            conn, tenant_id=tenant, raw=value, skill_dependencies=dependencies, actor="test-reviewer",
        )

    return _run(env, tenant, save)


def proposal(release, text="Improved business Prompt"):
    document = copy.deepcopy(release["document"])
    document["execution_plan"] = apply_changes(document["execution_plan"], [component_change(text)],
                                                 allowed_modules=["prompt"])
    document["origin"] = "candidate"
    return document


def publish(env, release, document, *, candidate="candidate-1", tenant="alpha"):
    return _run(env, tenant, lambda conn: ExecutionRepository(env.tx).publish(
        conn, tenant_id=tenant, profile_id="instructor", expected_release_id=release["release_id"],
        document=document, actor="reviewer", candidate_id=candidate, candidate_revision=1, validation_ref="check-1",
    ))


def test_registration_and_prompt_publication_append_only_independent_component_versions(pg_env):
    profile, baseline = register(pg_env)
    document = proposal(baseline)
    result = publish(pg_env, baseline, document)
    repo = ExecutionRepository(pg_env.tx)
    assert result["document"] == document
    assert result["composition_hash"] == composition_hash(document)
    current, loaded = _run(pg_env, "alpha", lambda conn: repo.current(conn, "instructor"))
    assert loaded["release_id"] == result["release_id"] and current["revision"] == profile["revision"] + 1
    original = _run(pg_env, "alpha", lambda conn: repo.release(conn, baseline["release_id"]))
    assert original["document"] == baseline["document"]

    async def inspect(conn):
        parts = await conn.fetch(
            f"SELECT component_id,revision,component FROM {pg_env.schema}.execution_asset_versions "
            "ORDER BY component_id,revision",
        )
        skills = await SkillLibraryRepository(pg_env.tx).list_skills(conn)
        return [dict(row) for row in parts], skills

    versions, skills = _run(pg_env, "alpha", inspect)
    assert [(row["component_id"], row["revision"]) for row in versions] == [
        ("main-prompt", 1), ("main-prompt", 2), ("shared-context", 1),
    ]
    assert {skill["name"]: skill["version"] for skill in skills} == {"demo": 1, "helper": 1}
    assert unpack(versions[0]["component"])["content"] == "Original business Prompt"


def test_profile_and_every_execution_table_enforce_rls_for_an_ordinary_runtime_role(pg_env):
    _, release = register(pg_env)
    repo = ExecutionRepository(pg_env.tx)
    receipt = {"release_id": release["release_id"], "composition_hash": release["composition_hash"],
               "request_id": "receipt-1", "state": "downloaded"}
    _run(pg_env, "alpha", lambda conn: repo.record_receipt(
        conn, tenant_id="alpha", profile_id="instructor", user_id="user-1", receipt=receipt,
    ))

    async def identity(conn):
        return await conn.fetchrow("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")

    role = _run(pg_env, "alpha", identity)
    assert not role["rolsuper"] and not role["rolbypassrls"]
    for table in ("execution_profiles", "execution_assets", "execution_asset_versions", "execution_releases",
                  "execution_receipts"):
        assert _run(pg_env, "beta", lambda conn, table=table: conn.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.{table}",
        )) == 0
    with pytest.raises(SkillNotFound):
        _run(pg_env, "beta", lambda conn: repo.profile(conn, "instructor"))
    with pytest.raises(SkillNotFound):
        _run(pg_env, "beta", lambda conn: repo.release(conn, release["release_id"]))
    with pytest.raises(Exception, match="row-level security"):
        _run(pg_env, "beta", lambda conn: conn.execute(
            f"INSERT INTO {pg_env.schema}.execution_profiles(tenant_id,profile_id,document) "
            "VALUES('alpha','forged','{}'::jsonb)",
        ))
    profile_b, _ = register(pg_env, tenant="beta")
    assert profile_b["profile_id"] == "instructor"


def test_profile_registration_conflicts_roll_back_without_partial_assets(pg_env):
    register(pg_env)
    raw = profile_raw("another-profile")
    with pytest.raises(ExecutionVersionConflict):
        register(pg_env, raw=raw)
    repo = ExecutionRepository(pg_env.tx)
    assert [p["profile_id"] for p in _run(pg_env, "alpha", repo.profiles)] == ["instructor"]
    assert _run(pg_env, "alpha", lambda conn: conn.fetchval(
        f"SELECT count(*) FROM {pg_env.schema}.execution_asset_versions",
    )) == 2


def test_freeze_scope_and_skill_dependencies_are_fixed_at_planning(pg_env):
    _, release = register(pg_env)
    frozen = _run(pg_env, "alpha", lambda conn: freeze_profile_scope(conn, pg_env.tx, "instructor", ["prompt"]))
    context, gap = _run(pg_env, "alpha", lambda conn: planning_context(
        conn, pg_env.tx, profile_id="instructor", skill_name="demo", allowed_modules=["prompt"], frozen=frozen,
    ))
    assert not gap and context["base_release_id"] == release["release_id"]
    assert _run(pg_env, "alpha", lambda conn: freeze_profile_scope(conn, pg_env.tx, "instructor", ["skill"])) is None
    _run(pg_env, "alpha", lambda conn: SkillLibraryRepository(pg_env.tx).commit_bundle(
        conn, tenant_id="alpha", name="helper", bundle={"SKILL.md": _skill_md("helper", "new")},
        mutation_id="new-helper", limits=BundleLimits(),
    ))
    _, gap = _run(pg_env, "alpha", lambda conn: planning_context(
        conn, pg_env.tx, profile_id="instructor", skill_name="demo", allowed_modules=["prompt"], frozen=frozen,
    ))
    assert gap == "execution_skill_dependency_changed"


def test_publish_idempotency_and_stale_baseline_do_not_overwrite_a_release(pg_env):
    _, baseline = register(pg_env)
    document = proposal(baseline)
    first = publish(pg_env, baseline, document)
    again = publish(pg_env, baseline, document)
    assert again["release_id"] == first["release_id"]
    with pytest.raises(ExecutionVersionConflict):
        publish(pg_env, baseline, proposal(baseline, "another edit"), candidate="candidate-2")
    with pytest.raises(ExecutionVersionConflict):
        publish(pg_env, baseline, proposal(baseline, "reuse ID with different content"))


def test_concurrent_same_publication_is_idempotent(pg_env):
    _, baseline = register(pg_env)
    document = proposal(baseline)
    repo = ExecutionRepository(pg_env.tx)

    async def race():
        ready = asyncio.Event()
        entered = 0

        async def one():
            nonlocal entered
            async with pg_env.tx.tenant_tx("alpha") as conn:
                entered += 1
                if entered == 2:
                    ready.set()
                await ready.wait()
                return await repo.publish(
                    conn, tenant_id="alpha", profile_id="instructor", expected_release_id=baseline["release_id"],
                    document=document, actor="reviewer", candidate_id="same-concurrent", candidate_revision=1,
                    validation_ref="check-1",
                )

        return await asyncio.gather(one(), one(), return_exceptions=True)

    results = pg_env.tx.pool.run(race())
    assert not any(isinstance(result, Exception) for result in results), results
    assert results[0]["release_id"] == results[1]["release_id"]


def test_receipts_are_user_bound_idempotent_and_explicitly_agent_reported(pg_env):
    _, release = register(pg_env)
    repo = ExecutionRepository(pg_env.tx)
    receipt = {"release_id": release["release_id"], "composition_hash": release["composition_hash"],
               "request_id": "download-once", "state": "downloaded"}

    def save(value, user="user-1"):
        return _run(pg_env, "alpha", lambda conn: repo.record_receipt(
            conn, tenant_id="alpha", profile_id="instructor", user_id=user, receipt=value,
        ))

    assert save(receipt) == save(receipt) == {**receipt, "evidence_level": "agent_reported"}
    with pytest.raises(ExecutionVersionConflict):
        save({**receipt, "state": "activated"})
    with pytest.raises(SkillNotFound):
        save(receipt, user="another-user")
    with pytest.raises(ExecutionVersionConflict):
        save({**receipt, "request_id": "bad-hash", "composition_hash": "0" * 64})


def test_concurrent_receipt_id_reuse_cannot_acknowledge_two_different_values(pg_env):
    _, release = register(pg_env)
    repo = ExecutionRepository(pg_env.tx)
    base = {"release_id": release["release_id"], "composition_hash": release["composition_hash"],
            "request_id": "same-concurrent"}

    async def race():
        ready = asyncio.Event()
        entered = 0

        async def one(state):
            nonlocal entered
            async with pg_env.tx.tenant_tx("alpha") as conn:
                entered += 1
                if entered == 2:
                    ready.set()
                await ready.wait()
                return await repo.record_receipt(conn, tenant_id="alpha", profile_id="instructor", user_id="user-1",
                                                 receipt={**base, "state": state})

        return await asyncio.gather(one("downloaded"), one("activated"), return_exceptions=True)

    results = pg_env.tx.pool.run(race())
    assert sum(isinstance(result, ExecutionVersionConflict) for result in results) == 1, results
    stored = _run(pg_env, "alpha", lambda conn: conn.fetchval(
        f"SELECT document FROM {pg_env.schema}.execution_receipts WHERE request_id='same-concurrent'",
    ))
    assert unpack(stored) == next(result for result in results if isinstance(result, dict))


def test_rollback_document_is_a_proposal_without_rewriting_immutable_history(pg_env):
    _, baseline = register(pg_env)
    current = publish(pg_env, baseline, proposal(baseline))
    repo = ExecutionRepository(pg_env.tx)
    profile, rollback = _run(pg_env, "alpha", lambda conn: repo.rollback_document(
        conn, "instructor", baseline["release_id"],
    ))
    assert profile["current_release_id"] == current["release_id"]
    assert rollback["execution_plan"]["components"][0]["content"] == "Original business Prompt"
    assert rollback["execution_plan"]["components"][0]["revision"] > 2
    after = _run(pg_env, "alpha", lambda conn: repo.release(conn, baseline["release_id"]))
    assert after["document"] == baseline["document"]
    assert len(_run(pg_env, "alpha", lambda conn: repo.releases(conn, "instructor"))) == 2


def test_profile_api_requires_existing_skills_and_returns_independent_plan_versions(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="execution-assets") as client:
        _login(client, bootstrap=True)
        assert client.post("/api/skill-evolution/execution-profiles", headers=H, json=profile_raw()).status_code == 400
        for name in ("demo", "helper"):
            response = client.post("/api/skills", headers=H, json={"name": name, "description": "Use for work",
                                                                  "body": "Unchanged Skill"})
            assert response.status_code == 200, response.text
        response = client.post("/api/skill-evolution/execution-profiles", headers=H, json=profile_raw())
        assert response.status_code == 201, response.text
        result = response.json()
        assert len(result["execution_plan"]["components"]) == 2
        assert result["execution_plan"]["components"][0]["revision"] == 1
        detail = client.get("/api/skill-evolution/execution-profiles/instructor", headers=H)
        assert detail.status_code == 200 and len(detail.json()["releases"]) == 1
        assert client.post("/api/skill-evolution/execution-profiles", headers=H, json=profile_raw()).status_code == 409
