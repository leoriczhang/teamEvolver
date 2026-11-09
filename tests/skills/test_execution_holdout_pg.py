"""Independent PG evidence for source-isolated, single-candidate locked acceptance."""
from __future__ import annotations

import asyncio
import copy
import json
import os

import pytest

from team_replay.datasets.repository import DatasetNotFound, DatasetRepository
from team_replay.datasets.schema import make_document
from team_skills.config import BundleLimits
from team_skills.execution.evolution import planning_context
from team_skills.execution.holdout import freeze_holdout, reserved_families
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.repository import SkillLibraryRepository
from tests.support.execution import profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


def dataset(env, identity="locked", *, tenant="alpha", provenance=None, role="locked_acceptance", revision=None,
            empty=False, check="SECRET_LOCKED_CHECK", query="SECRET_LOCKED_QUERY"):
    document = make_document(
        dataset_id=identity, name="Locked acceptance", skills=["demo", "helper"], allow_empty=empty,
        cases=[] if empty else [{"case_id": "private-case", "query": query, "skill_ids": ["demo", "helper"],
                                "checks": [{"id": "criterion", "kind": "output", "text": check}],
                                "provenance": {"source_session_key": "holdout-family"}
                                if provenance is None else provenance}],
        metadata={"validation_role": role},
    )
    return _run(env, tenant, lambda conn: DatasetRepository(env.tx).save(
        conn, tenant_id=tenant, document=document, expected_revision=revision,
    ))


def detail(env, ids=("locked",), *, tenant="alpha", profile="instructor"):
    async def seed(conn):
        library, dependencies = SkillLibraryRepository(env.tx), []
        for name in ("demo", "helper"):
            await library.commit_bundle(conn, tenant_id=tenant, name=name, bundle={"SKILL.md": _skill_md(name)},
                                        mutation_id="seed:" + name, limits=BundleLimits())
            head = await library.get_skill(conn, name)
            dependencies.append({"name": name, "version": head["version"], "tree_sha256": head["tree_sha256"]})
        raw = profile_raw(profile, prefix=profile + ":")
        raw["environment"]["validation"] = {"holdout_dataset_ids": list(ids), "strategy": "enhanced-v1"}
        _, release = await ExecutionRepository(env.tx).register(
            conn, tenant_id=tenant, raw=raw, skill_dependencies=dependencies, actor="independent-test",
        )
        return {"execution_profile_id": profile, "candidate_execution_plan": release["document"]["execution_plan"],
                "session_evidence": [{"provenance": {"source_session_key": "development-family"}}],
                "replay_cases": []}

    return _run(env, tenant, seed)


def freeze(env, value, *, candidate="candidate-one", revision=1, tenant="alpha"):
    return _run(env, tenant, lambda conn: freeze_holdout(
        conn, env.tx, tenant_id=tenant, detail=value, candidate_id=candidate, candidate_revision=revision,
    ))


def uses(env, tenant="alpha"):
    return _run(env, tenant, lambda conn: conn.fetch(
        f"SELECT profile_id,source_family,dataset_id,dataset_revision,candidate_id,candidate_revision "
        f"FROM {env.schema}.execution_holdout_uses ORDER BY source_family",
    ))


def assert_blocked(result, reason):
    report, cases = result
    assert report["status"] == "inconclusive"
    assert any(value.startswith(reason) for value in report["reasons"])
    assert cases == []


def test_same_candidate_revision_can_retry_but_other_candidates_or_revisions_are_exposed(pg_env):
    saved = dataset(pg_env)
    value = detail(pg_env)
    first, cases = freeze(pg_env, value)
    assert first["status"] == "ready" and first["reasons"] == []
    assert len(cases) == 1
    assert first["datasets"][0]["document_hash"] == saved["document_hash"]
    assert cases[0]["provenance"]["replay_selection"] == "locked_acceptance"
    again = freeze(pg_env, value)
    assert again == (first, cases)
    assert len(uses(pg_env)) == 1
    assert_blocked(freeze(pg_env, value, candidate="candidate-two"), "holdout_already_exposed:")
    assert_blocked(freeze(pg_env, value, revision=2), "holdout_already_exposed:")
    assert uses(pg_env)[0]["candidate_revision"] == 1


def test_dataset_new_revision_new_id_and_new_profile_cannot_hide_family_exposure(pg_env):
    dataset(pg_env)
    original = detail(pg_env)
    assert freeze(pg_env, original)[0]["status"] == "ready"
    dataset(pg_env, revision=1, check="REVISED_SECRET_CHECK")
    assert_blocked(freeze(pg_env, original, candidate="candidate-two"), "holdout_already_exposed:")
    dataset(pg_env, "renamed")
    changed = copy.deepcopy(original)
    changed["candidate_execution_plan"]["environment"]["validation"]["holdout_dataset_ids"] = ["renamed"]
    assert_blocked(freeze(pg_env, changed, candidate="candidate-two"), "holdout_already_exposed:")
    other_profile = detail(pg_env, ("renamed",), profile="another-profile")
    assert_blocked(freeze(pg_env, other_profile, candidate="candidate-two"), "holdout_already_exposed:")
    assert len(uses(pg_env)) == 1 and uses(pg_env)[0]["dataset_revision"] == 1


@pytest.mark.parametrize("field", ["session_evidence", "replay_cases"])
def test_diagnostic_and_development_source_families_are_disjoint_from_locked_acceptance(pg_env, field):
    dataset(pg_env, provenance={"source_session_keys": ["holdout-one", "shared-development"]})
    value = detail(pg_env)
    value[field] = [{"provenance": {"source_session_key": "shared-development"}}]
    assert_blocked(freeze(pg_env, value), "holdout_source_overlap:")
    assert uses(pg_env) == []


@pytest.mark.parametrize("provenance", [{}, {"source_session_keys": "not-an-array"},
                                        {"source_session_keys": {"unexpected": "object"}},
                                        {"source_session_key": 42}, {"source_session_keys": [None, ""]}])
def test_missing_or_malformed_source_identity_is_inconclusive_not_a_passing_holdout(pg_env, provenance):
    dataset(pg_env, provenance=provenance)
    assert_blocked(freeze(pg_env, detail(pg_env)), "holdout_source_unknown:")
    assert uses(pg_env) == []


@pytest.mark.parametrize("options,reason", [({"role": "development"}, "holdout_not_locked:"),
                                           ({"empty": True}, "holdout_source_unknown:")])
def test_unlocked_or_empty_datasets_never_count_as_independent_acceptance(pg_env, options, reason):
    dataset(pg_env, **options)
    assert_blocked(freeze(pg_env, detail(pg_env)), reason)
    assert uses(pg_env) == []


def test_reserved_planner_projection_has_only_family_ids_and_no_locked_queries_checks_or_cases(pg_env):
    dataset(pg_env, provenance={"source_session_key": "one", "source_session_keys": ["two", "one"]})
    value = detail(pg_env)
    result = _run(pg_env, "alpha", lambda conn: reserved_families(
        conn, pg_env.tx, value["candidate_execution_plan"],
    ))
    assert result == ["one", "two"]
    context, gap = _run(pg_env, "alpha", lambda conn: planning_context(
        conn, pg_env.tx, profile_id="instructor", skill_name="demo", allowed_modules=["prompt"],
    ))
    assert not gap and context["excluded_source_keys"] == ["one", "two"]
    serialized = json.dumps(context)
    assert "SECRET_LOCKED_QUERY" not in serialized and "SECRET_LOCKED_CHECK" not in serialized
    assert "private-case" not in serialized
    assert uses(pg_env) == []  # Reserving IDs for exclusion does not expose acceptance material.


@pytest.mark.parametrize("same_candidate", [False, True])
def test_concurrent_claims_are_serialized_across_the_entire_tenant(pg_env, same_candidate):
    dataset(pg_env)
    first = detail(pg_env)
    second = detail(pg_env, profile="another-profile")

    async def compete():
        ready, entered = asyncio.Event(), 0

        async def one(value, candidate):
            nonlocal entered
            async with pg_env.tx.tenant_tx("alpha") as conn:
                entered += 1
                if entered == 2:
                    ready.set()
                await ready.wait()
                return await freeze_holdout(conn, pg_env.tx, tenant_id="alpha", detail=value,
                                             candidate_id=candidate, candidate_revision=1)

        return await asyncio.gather(one(first, "one"), one(second, "one" if same_candidate else "two"))

    results = pg_env.tx.pool.run(compete())
    assert len(uses(pg_env)) == 1
    statuses = sorted(result[0]["status"] for result in results)
    assert statuses == (["ready", "ready"] if same_candidate else ["inconclusive", "ready"])
    if not same_candidate:
        blocked = next(result for result in results if result[0]["status"] == "inconclusive")
        assert_blocked(blocked, "holdout_already_exposed:")


def test_holdout_exposure_is_isolated_by_forced_rls_and_other_tenants_can_use_their_own_family(pg_env):
    dataset(pg_env)
    assert freeze(pg_env, detail(pg_env))[0]["status"] == "ready"
    assert uses(pg_env, "beta") == []
    with pytest.raises(DatasetNotFound):
        _run(pg_env, "beta", lambda conn: DatasetRepository(pg_env.tx).get(conn, "locked"))
    role = _run(pg_env, "beta", lambda conn: conn.fetchrow(
        "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user",
    ))
    assert not role["rolsuper"] and not role["rolbypassrls"]
    with pytest.raises(Exception, match="row-level security"):
        _run(pg_env, "beta", lambda conn: conn.execute(
            f"INSERT INTO {pg_env.schema}.execution_holdout_uses "
            "(tenant_id,profile_id,source_family,dataset_id,dataset_revision,candidate_id,candidate_revision) "
            "VALUES('alpha','instructor','forged','locked',1,'forged',1)",
        ))
    dataset(pg_env, tenant="beta")
    other = detail(pg_env, tenant="beta")
    assert freeze(pg_env, other, tenant="beta", candidate="beta-candidate")[0]["status"] == "ready"
    assert len(uses(pg_env, "alpha")) == len(uses(pg_env, "beta")) == 1


def _history_dataset(env, identity, *, metadata=None, source="evolution", skills=(), revision=None):
    document = make_document(
        dataset_id=identity, name=identity, skills=list(skills),
        cases=[{"case_id": "history-case", "query": "Inspect the recorded business behavior", "skill_ids": list(skills),
                "checks": [{"id": "behavior", "kind": "output", "text": "Keep the stated business constraints"}]}],
        metadata={"execution_profile_id": "history-profile", "baseline_execution_plan_hash": "a" * 64,
                  **(metadata or {})}, source={"kind": source},
    )
    return _run(env, "alpha", lambda conn: DatasetRepository(env.tx).save(
        conn, tenant_id="alpha", document=document, expected_revision=revision,
    ))


def _history_ids(env, *, skill=False):
    repo = DatasetRepository(env.tx)
    values = _run(env, "alpha", lambda conn: (
        repo.historical_cases(conn, skill_id="demo", before_dataset_id="history-current") if skill else
        repo.execution_history(conn, profile_id="history-profile", baseline_hash="a" * 64,
                               before_dataset_id="history-current")
    ))
    return {value["dataset_id"] for value in values}


def test_execution_history_boolean_opt_out_overrides_both_eligibility_paths(pg_env):
    flag = "evolution_history_sampling_disabled"
    matrix = [
        ("evolution-opted-out", "evolution", {flag: True, "enabled_for_evolution": False}, False),
        ("evolution-enabled-opted-out", "evolution", {flag: True, "enabled_for_evolution": True}, False),
        ("manual-opted-out", "manual", {flag: True, "enabled_for_evolution": True}, False),
        ("evolution-false", "evolution", {flag: False, "enabled_for_evolution": False}, True),
        ("evolution-missing", "evolution", {"enabled_for_evolution": False}, True),
        ("evolution-string-true", "evolution", {flag: "true", "enabled_for_evolution": False}, True),
        ("evolution-null", "evolution", {flag: None}, True),
        ("manual-enabled", "manual", {"enabled_for_evolution": True}, True),
        ("manual-still-disabled", "manual", {flag: False, "enabled_for_evolution": False}, False),
    ]
    for identity, source, metadata, _ in matrix:
        _history_dataset(pg_env, identity, source=source, metadata=metadata)
    _history_dataset(pg_env, "history-current")
    assert _history_ids(pg_env) == {identity for identity, _, _, included in matrix if included}


def test_execution_history_opt_out_uses_cas_head_without_deleting_old_revisions_or_listings(pg_env):
    repo = DatasetRepository(pg_env.tx)
    original = _history_dataset(pg_env, "preserved-history")
    _history_dataset(pg_env, "history-current")
    frozen = _run(pg_env, "alpha", lambda conn: repo.freeze(conn, "preserved-history", revision=1))
    assert _history_ids(pg_env) == {"preserved-history"}
    opted_out = _history_dataset(pg_env, "preserved-history", metadata={
        "evolution_history_sampling_disabled": True,
    }, revision=1)
    assert opted_out["revision"] == 2
    assert _history_ids(pg_env) == set()
    assert _run(pg_env, "alpha", lambda conn: repo.get(conn, "preserved-history", revision=1)) == original
    assert _run(pg_env, "alpha", lambda conn: repo.freeze(conn, "preserved-history", revision=1)) == frozen
    assert "preserved-history" in {row["dataset_id"] for row in _run(pg_env, "alpha", repo.list)}
    restored = _history_dataset(pg_env, "preserved-history", metadata={
        "evolution_history_sampling_disabled": False,
    }, revision=2)
    assert restored["revision"] == 3 and _history_ids(pg_env) == {"preserved-history"}
    assert _run(pg_env, "alpha", lambda conn: repo.get(conn, "preserved-history", revision=2)) == opted_out


def test_skill_history_sampling_keeps_its_existing_behavior_with_execution_opt_out_metadata(pg_env):
    _history_dataset(pg_env, "skill-history", skills=("demo",), metadata={
        "evolution_history_sampling_disabled": True, "enabled_for_evolution": False,
    })
    _history_dataset(pg_env, "history-current", skills=("demo",))
    assert _history_ids(pg_env, skill=True) == {"skill-history"}
