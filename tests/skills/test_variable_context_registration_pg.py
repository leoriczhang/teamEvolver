"""Isolated PG contracts for registration and immutable variable-asset versions.

Repository publication below checks storage/version semantics only. It does not
claim that a business Agent, Replay, review or publication gate has passed.
"""
from __future__ import annotations

import copy
import os

import pytest

from team_replay.execution_plan import render_components
from team_replay.variable_context import inline_variable_binding, source_value_hash
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.repository import unpack
from team_skills.execution.contracts import apply_changes
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.repository import SkillLibraryRepository
from tests.support.app import H, _login, _pod
from tests.support.execution import profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured"),
]

PROFILES = "/api/skill-evolution/execution-profiles"
RULE_A = "literal {{account}}\nrefuse first"
RULE_B = "Keep the second task condition"


def seed_source(env, *, tenant="default", with_candidate=False):
    async def seed(conn):
        library, repo = SkillLibraryRepository(env.tx), ExecutionRepository(env.tx)
        bundle = {"SKILL.md": _skill_md("demo")}
        await library.commit_bundle(conn, tenant_id=tenant, name="demo", bundle=bundle,
                                    mutation_id="source-skill", limits=BundleLimits())
        skill = await library.get_skill(conn, "demo")
        dependencies = [{"name": "demo", "version": skill["version"], "tree_sha256": skill["tree_sha256"]}]
        raw = profile_raw("source")
        raw["affected_skill_ids"] = ["demo"]
        raw["components"] = [{"component_id": "source-main", "kind": "agent_prompt", "slot": "system",
                              "order": 0, "content": "Account {{account}}. Task: {{taskRules}} / {{ taskRules }}."}]
        raw["environment"].update({
            "template_variables": {"account": "A", "taskRules": ""},
            "template_input_contract": {"schema_version": "teamevolver.template-input-contract.v1",
                                        "variable_names": ["account", "taskRules"]},
            "message_prefixes": {"user": "Sender：", "assistant": "Responder："},
            "custom_runtime_setting": {"preserve_exactly": [True, "value"]},
        })
        profile, release = await repo.register(conn, tenant_id=tenant, raw=raw,
                                               skill_dependencies=dependencies, actor="storage-contract")
        candidate = None
        if with_candidate:
            candidates = CandidateRepository(env.tx)
            candidate = await candidates.create(
                conn, tenant_id=tenant, skill_name="demo", bundle=bundle, limits=BundleLimits(),
                candidate_id="prior-open-candidate", detail={"execution_profile_id": "source"},
            )
            # Existing high-revision state is a storage fixture, not claimed
            # model-generated work or a fabricated successful evaluation.
            await conn.execute(f"UPDATE {env.schema}.skill_candidates SET revision=75 WHERE candidate_id=$1",
                               candidate["candidate_id"])
            candidate = await candidates.require(conn, candidate["candidate_id"])
        return profile, release, candidate

    return _run(env, tenant, seed)


def request_for(release, *, profile_id="derived"):
    return {"profile_id": profile_id, "source_release_id": release["release_id"],
            "managed_variables": ["taskRules"], "evolution_scope": "context", "variable_samples": [
                {"account": "A", "taskRules": RULE_A}, {"account": "B", "taskRules": RULE_A},
                {"account": "C", "taskRules": RULE_B}, {"account": "D", "taskRules": ""},
            ]}


def table_counts(env, tenant="default"):
    async def read(conn):
        tables = ("execution_profiles", "execution_assets", "execution_asset_versions", "execution_releases",
                  "skill_candidates", "skill_candidate_events", "worker_tasks")
        return {table: await conn.fetchval(f"SELECT count(*) FROM {env.schema}.{table}") for table in tables}

    return _run(env, tenant, read)


def current(env, profile_id="source", tenant="default"):
    return _run(env, tenant, lambda conn: ExecutionRepository(env.tx).current(conn, profile_id))


def test_preview_is_deterministic_without_persisting_profiles_assets_or_worker_tasks(pg_env, tmp_path, monkeypatch):
    _, source, _ = seed_source(pg_env)
    before = table_counts(pg_env)
    raw = request_for(source)
    with _pod(pg_env, tmp_path, monkeypatch, pod="variable-preview") as client:
        _login(client, bootstrap=True)
        path = PROFILES + "/source/variable-contexts/preview"
        first = client.post(path, headers=H, json=raw)
        second = client.post(path, headers=H, json=raw)
        assert first.status_code == second.status_code == 200, first.text
        assert first.json() == second.json()
        result = first.json()
        assert result["summary"]["sample_count"] == 4
        variable = result["summary"]["managed_variables"][0]
        assert variable["unique_values"] == 2 and variable["blank_samples"] == 1
        assert client.get(PROFILES + "/derived", headers=H).status_code == 404
    assert table_counts(pg_env) == before


def test_create_inherits_runtime_environment_and_skills_while_preserving_source_and_prior_candidate(
    pg_env, tmp_path, monkeypatch,
):
    profile, source, candidate = seed_source(pg_env, with_candidate=True)
    before = copy.deepcopy((profile, source, candidate))
    with _pod(pg_env, tmp_path, monkeypatch, pod="variable-create") as client:
        _login(client, bootstrap=True)
        response = client.post(PROFILES + "/source/variable-contexts", headers=H, json=request_for(source))
        assert response.status_code == 201, response.text
        registered = response.json()
        for key in ("runtime_type", "agent_id", "user_id", "affected_skill_ids"):
            assert registered[key] == profile[key]
        assert registered["skill_dependencies"] == source["document"]["skill_dependencies"]
        plan = registered["execution_plan"]
        expected_env = copy.deepcopy(source["document"]["execution_plan"]["environment"])
        inline = [p for p in plan["components"] if inline_variable_binding(p)]
        expected_env.update({
            "evolution": {"editable_component_ids": sorted(p["component_id"] for p in inline)},
            "variable_context_source": {"profile_id": "source", "release_id": source["release_id"],
                                        "composition_hash": source["composition_hash"]},
        })
        assert plan["environment"] == expected_env
        assert len(inline) == 2
        assert {p["binding"]["inline_variable"]["source_value_hash"] for p in inline} == {
            source_value_hash(RULE_A), source_value_hash(RULE_B),
        }
        assert all(int(p["revision"]) == 1 for p in plan["components"])
        assert plan["components"][0]["content"] == source["document"]["execution_plan"]["components"][0]["content"]

    async def versions(conn):
        rows = await conn.fetch(f"SELECT component_id,revision,component FROM {pg_env.schema}.execution_asset_versions "
                                "WHERE component_id=ANY($1::text[]) ORDER BY component_id",
                                [p["component_id"] for p in plan["components"]])
        return [{**dict(r), "component": unpack(r["component"])} for r in rows]

    stored = _run(pg_env, "default", versions)
    assert len(stored) == 3 and all(r["revision"] == 1 for r in stored)
    assert {r["component_id"]: r["component"] for r in stored} == {p["component_id"]: p for p in plan["components"]}
    saved_profile, saved_source = current(pg_env)
    saved_candidate = _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).require(
        conn, candidate["candidate_id"],
    ))
    assert (saved_profile, saved_source, saved_candidate) == before
    assert table_counts(pg_env)["worker_tasks"] == 0


def test_duplicate_ids_and_tenant_isolation_do_not_leak_or_overwrite_baselines(pg_env, tmp_path, monkeypatch):
    _, source, _ = seed_source(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="variable-tenant") as client:
        _login(client, bootstrap=True)
        path = PROFILES + "/source/variable-contexts"
        response = client.post(path, headers=H, json=request_for(source))
        assert response.status_code == 201, response.text
        original = current(pg_env, "derived")
        counts = table_counts(pg_env)
        duplicate = client.post(path, headers=H, json=request_for(source))
        assert duplicate.status_code == 409, duplicate.text
        assert table_counts(pg_env) == counts and current(pg_env, "derived") == original
        alpha_headers = {"X-Tenant-Id": "alpha"}
        assert client.get(PROFILES + "/source", headers=alpha_headers).status_code == 404
        assert client.post(path + "/preview", headers=alpha_headers, json=request_for(source)).status_code == 404
        assert table_counts(pg_env, "alpha")["execution_profiles"] == 0
        _, alpha_source, _ = seed_source(pg_env, tenant="alpha")
        alpha = client.post(path, headers=alpha_headers, json=request_for(alpha_source))
        assert alpha.status_code == 201, alpha.text
        assert current(pg_env, "derived") == original
        assert current(pg_env, "derived", "alpha")[1]["release_id"] != original[1]["release_id"]


def test_stale_source_release_is_rejected_before_preview_or_create_persists_anything(pg_env, tmp_path, monkeypatch):
    _, source, _ = seed_source(pg_env)
    document = copy.deepcopy(source["document"])
    document["execution_plan"] = apply_changes(document["execution_plan"], [{
        "component_id": "source-main",
        "content": document["execution_plan"]["components"][0]["content"] + " New wording.",
        "reason": "Storage concurrency contract only", "evidence_refs": ["synthetic-storage-fixture"],
    }], allowed_modules=["prompt"])
    _run(pg_env, "default", lambda conn: ExecutionRepository(pg_env.tx).publish(
        conn, tenant_id="default", profile_id="source", expected_release_id=source["release_id"],
        document=document, actor="storage-contract", candidate_id="source-storage-change", candidate_revision=1,
        validation_ref="storage-only-no-business-replay",
    ))
    counts = table_counts(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="variable-cas") as client:
        _login(client, bootstrap=True)
        for suffix in ("/preview", ""):
            response = client.post(PROFILES + "/source/variable-contexts" + suffix,
                                   headers=H, json=request_for(source))
            assert response.status_code == 409 and "source release changed" in response.text
    assert table_counts(pg_env) == counts


def test_context_publish_and_rollback_append_versions_keep_selectors_and_restore_literal_rendering(
    pg_env, tmp_path, monkeypatch,
):
    _, source, _ = seed_source(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="variable-version") as client:
        _login(client, bootstrap=True)
        response = client.post(PROFILES + "/source/variable-contexts", headers=H, json=request_for(source))
        assert response.status_code == 201, response.text
    repo = ExecutionRepository(pg_env.tx)
    _, baseline = current(pg_env, "derived")
    original = copy.deepcopy(baseline["document"])
    asset = next(p for p in original["execution_plan"]["components"] if p["content"] == RULE_A)
    new_text = "Revised literal {{account}} rule"
    document = copy.deepcopy(original)
    document["execution_plan"] = apply_changes(document["execution_plan"], [{
        "component_id": asset["component_id"], "content": new_text,
        "reason": "Storage version contract only", "evidence_refs": ["synthetic-storage-fixture"],
    }], allowed_modules=["prompt"])
    published = _run(pg_env, "default", lambda conn: repo.publish(
        conn, tenant_id="default", profile_id="derived", expected_release_id=baseline["release_id"],
        document=document, actor="storage-contract", candidate_id="context-storage-change", candidate_revision=1,
        validation_ref="storage-only-no-business-replay",
    ))
    _, rollback = _run(pg_env, "default", lambda conn: repo.rollback_document(conn, "derived", baseline["release_id"]))
    restored = _run(pg_env, "default", lambda conn: repo.publish(
        conn, tenant_id="default", profile_id="derived", expected_release_id=published["release_id"],
        document=rollback, actor="storage-contract", candidate_id="context-storage-rollback", candidate_revision=1,
        validation_ref="storage-only-no-business-replay", restored_from=baseline["release_id"],
    ))

    def render(plan, value):
        plan = copy.deepcopy(plan)
        plan["environment"]["template_variables"] = {"account": "A", "taskRules": value}
        return render_components(plan)[0]["content"]

    for value in (RULE_A, RULE_B, "", "Unregistered original input"):
        before = render(original["execution_plan"], value)
        assert render(restored["document"]["execution_plan"], value) == before
        after = render(published["document"]["execution_plan"], value)
        assert after == (f"Account A. Task: {new_text} / {new_text}." if value == RULE_A else before)
    assert render(original["execution_plan"], RULE_A) == f"Account A. Task: {RULE_A} / {RULE_A}."
    immutable = _run(pg_env, "default", lambda conn: repo.release(conn, baseline["release_id"]))
    assert immutable["document"] == original
    rows = _run(pg_env, "default", lambda conn: conn.fetch(
        f"SELECT revision,component FROM {pg_env.schema}.execution_asset_versions "
        "WHERE component_id=$1 ORDER BY revision", asset["component_id"],
    ))
    assert [r["revision"] for r in rows] == [1, 2, 3]
    components = [unpack(r["component"]) for r in rows]
    assert [p["content"] for p in components] == [RULE_A, new_text, RULE_A]
    assert all(p["binding"] == asset["binding"] for p in components)
    assert current(pg_env)[1] == source
