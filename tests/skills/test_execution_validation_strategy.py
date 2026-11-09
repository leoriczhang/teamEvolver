"""Standard publication uses the main frozen Replay; legacy plans remain enhanced."""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from team_skills.evolution import candidate_pipeline
from team_skills.execution import holdout, reviewed_publication, validation
from team_skills.execution.candidates import publication_gate
from team_skills.execution.contracts import apply_changes, plan_for, validate_profile
from team_skills.execution.costs import budget_gate
from team_skills.execution.holdout import (
    ENHANCED_VALIDATION,
    STANDARD_VALIDATION,
    candidate_validation_policy,
    enhanced_validation,
    reserved_families,
    validation_policy,
)
from team_skills.execution.probes import probe_jobs, run_probes
from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import component_change, profile_raw


def detail(*, joint=False, strategy=None, maximum="absent"):
    raw = profile_raw()
    raw["affected_skill_ids"] = ["demo"] if joint else []
    options = {}
    if strategy is not None:
        options["strategy"] = strategy
    if maximum != "absent":
        options["max_total_tokens"] = maximum
    if options:
        raw["environment"]["validation"] = options
    profile = validate_profile(raw)
    left = plan_for(profile, profile["components"])
    right = apply_changes(left, [component_change()], allowed_modules=["prompt"])
    left["target_component_ids"] = right["target_component_ids"]
    return {
        "candidate_kind": "skill" if joint else "execution", "execution_profile_id": "instructor",
        "allowed_modules": ["skill", "prompt"] if joint else ["prompt"],
        "actual_modules": ["skill", "prompt"] if joint else ["prompt"],
        "base_release_id": "baseline", "baseline_execution_plan": left, "candidate_execution_plan": right,
        "affected_skill_ids": raw["affected_skill_ids"], "skill_changed": joint,
        "skill_dependencies": [{"name": "demo", "version": 1, "tree_sha256": "frozen-tree"}] if joint else [],
        "replay_cases": [{"case_id": "current", "skill_ids": ["demo"] if joint else [],
                          "provenance": {"replay_selection": "current"}}],
    }


def repositories(monkeypatch, value):
    class Execution:
        def __init__(self, tx):
            pass

        async def current(self, conn, identity):
            return {"current_release_id": "baseline", "affected_skill_ids": value["affected_skill_ids"]}, {}

    class Library:
        def __init__(self, tx):
            pass

        async def get_version(self, conn, name, revision):
            return {"tree_sha256": "frozen-tree"}

        async def read_version_bundle(self, conn, name, revision):
            return {"SKILL.md": b"---\nname: demo\ndescription: Frozen peer\n---\nBody\n"}

    monkeypatch.setattr(validation, "ExecutionRepository", Execution)
    monkeypatch.setattr(validation, "SkillLibraryRepository", Library)


@pytest.mark.asyncio
@pytest.mark.parametrize("joint", [False, True])
async def test_new_standard_freezes_main_replay_policy_without_locked_cases_or_implicit_cost_gate(monkeypatch, joint):
    value = detail(joint=joint)
    before = copy.deepcopy(value)
    repositories(monkeypatch, value)

    async def forbidden(*args, **kwargs):
        pytest.fail("standard validation must not claim locked acceptance")

    monkeypatch.setattr(validation, "freeze_holdout", forbidden)
    frozen = await validation.freeze_execution_validation(None, None, value)
    plan = frozen["validation_plan"]
    assert plan["validation_strategy"] == STANDARD_VALIDATION
    assert plan["independent_verification_required"] is False
    assert plan["holdout"]["status"] == "not_required"
    assert plan["probe_policy"]["max_total_tokens"] is None
    assert frozen["replay_cases"] == value["replay_cases"]
    assert frozen["candidate_execution_plan"] == validation.validate_execution_plan_pair(
        value["baseline_execution_plan"], value["candidate_execution_plan"])[1]
    assert value == before
    assert candidate_validation_policy(value)["max_total_tokens"] == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("override,expected", [(False, 1234), (True, None)])
async def test_standard_keeps_explicit_finite_or_uncapped_authority_without_mutating_agent_inputs(
    monkeypatch, override, expected,
):
    value = detail(maximum=1234)
    if override:
        value["validation_budget"] = {"max_total_tokens": None, "previous_max_total_tokens": 1234,
                                      "reason": "Explicit owner authorization"}
    repositories(monkeypatch, value)
    before = copy.deepcopy(value)
    frozen = await validation.freeze_execution_validation(None, None, value)
    assert frozen["validation_plan"]["probe_policy"]["max_total_tokens"] == expected
    assert value == before


@pytest.mark.asyncio
async def test_explicit_enhanced_strategy_still_claims_holdout_and_requires_independent_evidence(monkeypatch):
    value = detail(strategy=ENHANCED_VALIDATION)
    repositories(monkeypatch, value)
    calls = []

    async def holdout(*args, **kwargs):
        calls.append(kwargs["detail"])
        return {"status": "ready", "policy": candidate_validation_policy(value)}, [{"case_id": "locked"}]

    monkeypatch.setattr(validation, "freeze_holdout", holdout)
    frozen = await validation.freeze_execution_validation(None, None, value)
    assert len(calls) == 1 and frozen["replay_cases"][-1] == {"case_id": "locked"}
    assert frozen["validation_plan"]["validation_strategy"] == ENHANCED_VALIDATION
    assert frozen["validation_plan"]["independent_verification_required"] is True
    assert frozen["validation_plan"]["probe_policy"]["max_total_tokens"] == 1_000_000


@pytest.mark.parametrize("strategy", [None, "", "standard", [], True])
def test_invalid_explicit_strategy_is_rejected_without_guessing_a_default(strategy):
    with pytest.raises(SkillLibraryError):
        validation_policy({"validation": {"strategy": strategy}})
    with pytest.raises(SkillLibraryError):
        enhanced_validation({"validation_plan": {"validation_strategy": strategy}})


def standard_result(*, joint=False, budget=None):
    value = detail(joint=joint)
    value["validation_plan"] = {
        "validation_strategy": STANDARD_VALIDATION, "input_hash": "frozen-input",
        "probe_policy": {"max_total_tokens": budget}, "holdout": {"status": "not_required"},
    }
    result = {"verdict": "accept", "runtime_gate": {"status": "passed"},
              "execution_gate": {"status": "passed"}, "cost_gate": {"status": "passed"}}
    return value, result


@pytest.mark.parametrize("joint", [False, True])
def test_standard_accepts_main_success_without_enhanced_artifacts_or_complete_uncapped_usage(joint):
    value, result = standard_result(joint=joint)
    result["cost_gate"] = budget_gate(value, result)
    assert result["cost_gate"]["status"] == "passed"
    assert result["cost_gate"]["total_tokens"] is None
    assert result["cost_gate"]["accounting_status"] == "incomplete"
    assert publication_gate(value, result) == {"passed": True, "reasons": []}
    assert not {"probe_gate", "independent_validation"} & result.keys()


@pytest.mark.parametrize("verdict", ["reject", "inconclusive", None])
def test_standard_never_promotes_a_failed_or_missing_main_replay(verdict):
    value, result = standard_result()
    result["verdict"] = verdict
    assert "main_replay_not_accepted" in publication_gate(value, result)["reasons"]


def test_standard_keeps_loading_input_binding_regression_coverage_and_finite_budget_constraints():
    value, result = standard_result(joint=True, budget=1000)
    result["cost_gate"] = {"status": "inconclusive", "reasons": ["total_model_cost_exceeds_budget"]}
    assert "validation_budget_unverified" in publication_gate(value, result)["reasons"]
    result["cost_gate"]["status"] = "passed"
    result["execution_gate"]["status"] = "inconclusive"
    assert "execution_not_observed" in publication_gate(value, result)["reasons"]
    result["execution_gate"]["status"] = "passed"
    value["replay_cases"][0]["skill_ids"] = []
    assert "regression_coverage_missing" in publication_gate(value, result)["reasons"]
    value["validation_plan"].pop("input_hash")
    assert "validation_inputs_missing" in publication_gate(value, result)["reasons"]


def test_legacy_missing_flag_does_not_inherit_the_new_environment_default_or_relaxed_gate():
    value, result = standard_result()
    value["validation_plan"].pop("validation_strategy")
    before = copy.deepcopy(value)
    assert validation_policy(value["candidate_execution_plan"]["environment"])["strategy"] == STANDARD_VALIDATION
    assert enhanced_validation(value) is True
    rejected = publication_gate(value, result)
    assert {"independent_validation_missing", "execution_probes_incomplete"} <= set(rejected["reasons"])
    assert value == before
    legacy_job = {**value, "job_id": "legacy", "current_skill": {}, "candidate_skill": {}}
    assert len(probe_jobs(legacy_job)[0]) >= 2


@pytest.mark.asyncio
async def test_standard_does_not_dispatch_probes_or_reserve_locked_source_families():
    value, result = standard_result()

    async def forbidden(*args):
        pytest.fail("no additional Replay is allowed for standard validation")

    assert probe_jobs(value) == ([], [])
    assert await run_probes(value, result, forbidden) == {"status": "not_required"}
    assert await reserved_families(None, None, value["candidate_execution_plan"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,expected", [(None, ["locked-family"]),
                                               (STANDARD_VALIDATION, []),
                                               (ENHANCED_VALIDATION, ["locked-family"])])
async def test_old_generation_source_exclusions_are_not_backfilled_with_the_new_standard_default(
    monkeypatch, strategy, expected,
):
    value = detail(strategy=strategy)
    value["candidate_execution_plan"]["environment"].setdefault("validation", {})["holdout_dataset_ids"] = ["locked"]
    reads = []

    class Datasets:
        def __init__(self, tx):
            pass

        async def get(self, conn, dataset_id):
            reads.append(dataset_id)
            return {"cases": [{"provenance": {"source_session_key": "locked-family"}}]}

    monkeypatch.setattr(holdout, "DatasetRepository", Datasets)
    assert await reserved_families(None, None, value["candidate_execution_plan"]) == expected
    assert reads == (["locked"] if expected else [])


@pytest.mark.asyncio
async def test_retry_of_an_already_accepted_legacy_command_does_not_freeze_a_new_standard_plan(monkeypatch):
    request = {"candidate_id": "candidate", "revision": 1, "mode": "evaluate"}
    existing = {"request_hash": candidate_pipeline.digest(request),
                "payload": {"detail": {"validation_plan": {"independent_verification_required": True}}}}
    before = copy.deepcopy(existing)

    class Candidates:
        def __init__(self, tx):
            pass

        async def require(self, *args, **kwargs):
            return {"revision": 1}

    class Commands:
        def __init__(self, tx):
            pass

        async def command(self, *args):
            return existing

    async def lock(*args):
        pass

    async def forbidden(*args, **kwargs):
        pytest.fail("a saved command must not be refrozen under the new default")

    monkeypatch.setattr(candidate_pipeline, "CandidateRepository", Candidates)
    monkeypatch.setattr(candidate_pipeline, "EvolutionRepository", Commands)
    monkeypatch.setattr(candidate_pipeline, "lock_key", lock)
    monkeypatch.setattr(candidate_pipeline, "freeze_execution_validation", forbidden)
    result = await candidate_pipeline.submit_validation(
        None, SimpleNamespace(tx=None), tenant_id="default", candidate_id="candidate", command_id="old-command",
        config=None, actor="administrator", revision=1)
    assert result is existing and existing == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "main_reject", "main_inconclusive", "loading", "static", "stale_env",
                                       "legacy_missing_flag"])
async def test_administrator_publication_uses_one_completed_main_run_and_never_publishes_bad_evidence(
    monkeypatch, fault,
):
    value, replay = standard_result()
    if fault == "legacy_missing_flag":
        value["validation_plan"].pop("validation_strategy")
    verdict = "reject" if fault == "main_reject" else "inconclusive" if fault == "main_inconclusive" else "accept"
    replay["verdict"] = verdict
    if fault == "loading":
        replay["execution_gate"]["status"] = "inconclusive"
    evaluation = {"validation_verdict": verdict, "static_validation": {"passed": fault != "static"},
                  "replay": replay}
    frozen = {"detail": value, "tree_sha256": bundle_tree_sha256({}), "baseline": None,
              "config_revision": 1, "config": {}}
    saved = {"payload": frozen, "evaluation": evaluation, "validation_run_id": "completed-main",
             "validation_payload_hash": "payload-hash", "validation_result_hash": "result-hash"}
    item = {"candidate_id": "candidate", "revision": 1, "status": "open", "detail": copy.deepcopy(value)}
    events, published, results = [{"seq": 1}], [], []

    @asynccontextmanager
    async def transaction(*args):
        yield conn

    conn = SimpleNamespace(transaction=transaction)
    tx = SimpleNamespace(tenant_tx=transaction)

    class Repository:
        def __init__(self, tx):
            pass

        async def require(self, *args, **kwargs):
            return copy.deepcopy(item)

        async def events(self, *args):
            return events

        @staticmethod
        def cost_state_hash(item):
            return "unchanged-state"

        async def read_bundle(self, *args):
            return {}

        async def transition(self, *args, **kwargs):
            assert kwargs["to_status"] == "published"

    class History:
        def __init__(self, tx):
            pass

        async def finish(self, conn, identity, result):
            results.append(result)

    class Experiences:
        def __init__(self, tx):
            pass

        async def candidate_outcome(self, *args, **kwargs):
            pass

    async def current(*args, **kwargs):
        if fault == "stale_env":
            raise SkillLibraryError("frozen environment changed")

    async def completed(*args):
        return copy.deepcopy(saved)

    async def publish(*args, **kwargs):
        published.append(kwargs)
        assert kwargs["payload"]["command_id"] == "completed-main"
        return {"release_id": "release:published", "version": None}

    async def guard(*args):
        pass

    monkeypatch.setattr(reviewed_publication, "CandidateRepository", Repository)
    monkeypatch.setattr(reviewed_publication, "EvolutionRepository", History)
    monkeypatch.setattr(reviewed_publication, "ExperienceRepository", Experiences)
    monkeypatch.setattr(reviewed_publication, "completed_validation", completed)
    monkeypatch.setattr(reviewed_publication, "require_current_environment", current)
    monkeypatch.setattr(reviewed_publication, "publish_candidate", publish)
    payload = {"candidate_id": "candidate", "revision": 1, "actor": "administrator", "mode": "auto",
               "candidate_state_hash": "unchanged-state", "candidate_event_seq": 1,
               **{key: saved[key] for key in (
                   "validation_run_id", "validation_payload_hash", "validation_result_hash")}}
    services = SimpleNamespace(tx=tx, tasks=SimpleNamespace(require_current=guard))
    task = SimpleNamespace(task_id="publish-command", tenant_id="default")
    await reviewed_publication.publish_reviewed_command(task, services, payload)
    assert len(results) == 1
    assert results[0]["status"] == ("published" if fault is None else "blocked")
    assert len(published) == (1 if fault is None else 0)
