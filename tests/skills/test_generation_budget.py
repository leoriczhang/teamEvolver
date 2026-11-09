"""One-command generation authority is explicit, immutable and Prompt-scoped."""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from team_skills.api import candidates
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.execution.holdout import (
    candidate_validation_policy,
    freeze_generation_budget,
    generation_budget_policy,
    validation_policy,
)
from team_skills.library.repository import SkillLibraryError
from tests.support.execution import profile_raw


def _baseline(maximum=1000):
    raw = profile_raw("registered-profile")
    raw["environment"]["validation"] = {"max_total_tokens": maximum}
    profile = validate_profile(raw)
    return {"status": "frozen", "profile": profile, "release": {
        "release_id": "published-baseline", "composition_hash": "unchanged-composition",
        "document": {"execution_plan": plan_for(profile, profile["components"]),
                     "skill_dependencies": [{"name": "demo", "version": 1, "tree_sha256": "fixed-tree"}]},
    }}


@pytest.mark.parametrize("maximum", [None, 1001, 10_000_000])
def test_freeze_generation_authority_raises_or_removes_only_the_generation_ceiling(maximum):
    baseline = _baseline()
    raw = {"max_total_tokens": maximum, "reason": "The owner authorized this generation attempt"}
    before = copy.deepcopy((baseline, raw))
    authorization = freeze_generation_budget(raw, frozen_baseline=baseline, allowed_modules=["prompt"])
    assert authorization == {**raw, "previous_max_total_tokens": 1000}
    plan = baseline["release"]["document"]["execution_plan"]
    policy = generation_budget_policy(plan, authorization)
    assert policy == {**validation_policy(plan["environment"]), "max_total_tokens": maximum}
    assert (baseline, raw) == before
    assert plan["environment"]["validation"]["max_total_tokens"] == 1000
    authorization["reason"] = "Changed external copy"
    assert raw["reason"] == before[1]["reason"]


def test_missing_authority_preserves_finite_defaults_and_does_not_mutate_profile_lists():
    baseline = _baseline()
    plan = baseline["release"]["document"]["execution_plan"]
    before = copy.deepcopy(plan)
    policy = generation_budget_policy(plan)
    assert policy == validation_policy(plan["environment"])
    policy["holdout_dataset_ids"].append("caller-only")
    assert plan == before
    assert generation_budget_policy({"environment": {}})["max_total_tokens"] == 1_000_000


@pytest.mark.parametrize("maximum", [True, False, 0, -1, 1000, 999, 10_000_001, 1.5, "null", "unlimited", {}])
def test_generation_authority_cannot_lower_equal_coerce_or_exceed_supported_finite_ceiling(maximum):
    with pytest.raises(SkillLibraryError):
        freeze_generation_budget({"max_total_tokens": maximum, "reason": "Explicit authorization"},
                                 frozen_baseline=_baseline(), allowed_modules=["prompt"])


@pytest.mark.parametrize("reason", [None, "", "  ", True, "x" * 2001])
def test_generation_authority_requires_a_bounded_nonempty_audit_reason(reason):
    with pytest.raises(SkillLibraryError):
        freeze_generation_budget({"max_total_tokens": None, "reason": reason},
                                 frozen_baseline=_baseline(), allowed_modules=["skill", "prompt"])


@pytest.mark.parametrize("scope", [["skill"], [], None, "prompt"])
def test_generation_authority_cannot_change_skill_only_commands(scope):
    with pytest.raises(SkillLibraryError):
        freeze_generation_budget({"max_total_tokens": None, "reason": "No Prompt authority"},
                                 frozen_baseline=_baseline(), allowed_modules=scope)


@pytest.mark.parametrize("raw", [None, {}, {"reason": "No amount"}, {"max_total_tokens": None},
                                  {"max_total_tokens": None, "reason": "Forged prior", "previous_max_total_tokens": 1}])
def test_generation_request_cannot_choose_its_own_previous_limit_or_omit_authority_fields(raw):
    with pytest.raises(SkillLibraryError):
        freeze_generation_budget(raw, frozen_baseline=_baseline(), allowed_modules=["prompt"])


@pytest.mark.parametrize("baseline", [None, {}, {"status": "execution_profile_missing"},
                                      {"status": "frozen", "release": None},
                                      {"status": "frozen", "release": {"document": {"execution_plan": {}}}}])
def test_generation_authority_requires_an_actual_frozen_prompt_baseline(baseline):
    with pytest.raises(SkillLibraryError):
        freeze_generation_budget({"max_total_tokens": None, "reason": "Requires frozen baseline"},
                                 frozen_baseline=baseline, allowed_modules=["prompt"])


def test_worker_revalidates_frozen_previous_budget_and_candidate_history_behavior_is_unchanged():
    plan = _baseline()["release"]["document"]["execution_plan"]
    historical = {"max_total_tokens": None, "previous_max_total_tokens": 2000, "reason": "Earlier increase"}
    # Existing Candidate validation supports earlier finite increases; one new
    # generation command must instead bind exactly to its accepted baseline.
    assert candidate_validation_policy({"candidate_execution_plan": plan,
                                        "validation_budget": historical})["max_total_tokens"] is None
    with pytest.raises(SkillLibraryError, match="generation budget authorization"):
        generation_budget_policy(plan, historical)


@pytest.fixture
def trigger_acceptance(monkeypatch):
    row = SimpleNamespace(revision=7, config={"evolution_execution_profile_id": "registered-profile"})
    configs = SimpleNamespace(read_tenant_module=AsyncMock(return_value=row), write_tenant_module=AsyncMock())
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=datetime(2026, 11, 2, tzinfo=timezone.utc)))

    @asynccontextmanager
    async def tenant_tx(_tenant):
        yield conn

    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx, pool=SimpleNamespace(schema="test")),
                               extras={"config_repo": configs})
    baseline, queued = _baseline(), []

    async def freeze(_conn, _tx, _profile_id, allowed_modules):
        return baseline if "prompt" in allowed_modules else None

    async def enqueue(*args, **kwargs):
        queued.append(copy.deepcopy(kwargs))
        return {"command_id": kwargs["command_id"]}, True

    monkeypatch.setattr(candidates, "actor", lambda request: "authorized-operator")
    monkeypatch.setattr(candidates, "current_tenant_id", lambda: "default")
    monkeypatch.setattr(candidates, "freeze_profile_scope", AsyncMock(side_effect=freeze))
    monkeypatch.setattr(candidates, "enqueue_command", enqueue)
    app = FastAPI()
    app.include_router(candidates.create_router(services))
    return SimpleNamespace(app=app, baseline=baseline, row=row, queued=queued, configs=configs)


@pytest.mark.asyncio
async def test_trigger_without_authorization_retains_legacy_skill_payload_and_default_prompt_budget(trigger_acceptance):
    env = trigger_acceptance
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
        assert (await client.post("/api/skill-evolution/trigger", json={})).status_code == 202
        legacy = env.queued[-1]["payload"]
        assert legacy["allowed_modules"] == ["skill"] and legacy["execution_baseline"] is None
        assert "generation_budget" not in legacy
        response = await client.post("/api/skill-evolution/trigger", json={"allowed_modules": ["prompt"]})
        assert response.status_code == 202
        prompt = env.queued[-1]["payload"]
        assert "generation_budget" not in prompt
        plan = prompt["execution_baseline"]["release"]["document"]["execution_plan"]
        assert generation_budget_policy(plan)["max_total_tokens"] == 1000
    env.configs.write_tenant_module.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [None, 2000])
async def test_trigger_freezes_explicit_authority_with_native_actor_reason_and_unchanged_release(
    trigger_acceptance, maximum,
):
    env = trigger_acceptance
    before = copy.deepcopy(env.baseline)
    raw = {"allowed_modules": ["prompt", "skill"], "execution_profile_id": "registered-profile",
           "generation_budget": {"max_total_tokens": maximum, "reason": "User authorized this run"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
        response = await client.post("/api/skill-evolution/trigger", json=raw,
                                     headers={"Idempotency-Key": "generation-authority-test"})
        assert response.status_code == 202, response.text
    queued = env.queued[-1]
    payload = queued["payload"]
    assert queued["request"] == raw
    assert payload["generation_budget"] == {**raw["generation_budget"], "previous_max_total_tokens": 1000}
    assert payload["actor"] == "authorized-operator" and payload["config_revision"] == 7
    assert payload["accepted_at"] and payload["allowed_modules"] == ["skill", "prompt"]
    assert payload["execution_baseline"] == env.baseline == before
    assert env.row.config == {"evolution_execution_profile_id": "registered-profile"}
    raw["generation_budget"]["reason"] = "Later caller mutation"
    env.baseline["release"]["document"]["execution_plan"]["environment"]["validation"]["max_total_tokens"] = 9
    assert payload["generation_budget"]["reason"] == "User authorized this run"
    assert payload["generation_budget"]["previous_max_total_tokens"] == 1000
    assert payload["execution_baseline"] == before
    env.configs.write_tenant_module.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"generation_budget": {"max_total_tokens": None, "reason": "Not a Prompt command"}},
    {"allowed_modules": ["prompt"], "generation_budget": None},
    {"allowed_modules": ["prompt"], "generation_budget": {"max_total_tokens": True, "reason": "Invalid"}},
    {"allowed_modules": ["prompt"], "generation_budget": {"max_total_tokens": 1000, "reason": "No increase"}},
    {"allowed_modules": ["prompt"], "generation_budget": {"max_total_tokens": None, "reason": " "}},
    {"allowed_modules": ["prompt"], "generation_budget": {"max_total_tokens": None, "reason": "Forged",
                                                         "previous_max_total_tokens": 999}},
])
async def test_trigger_rejects_invalid_generation_authority_without_enqueuing(trigger_acceptance, body):
    env = trigger_acceptance
    before = copy.deepcopy(env.baseline)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
        response = await client.post("/api/skill-evolution/trigger", json=body)
        assert response.status_code == 400, response.text
    assert not env.queued and env.baseline == before
    env.configs.write_tenant_module.assert_not_awaited()
