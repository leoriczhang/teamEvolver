"""The production planner resolves its tenant/config model only when dispatched."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from team_skills.config import SkillsModuleConfig
from team_skills.evolution import cycle, model
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


@pytest.mark.asyncio
async def test_actual_composition_planner_uses_frozen_tenant_model_without_eager_resolution(
    monkeypatch,
):
    calls, dispatched, client = [], [], object()

    async def resolve(services, tenant, settings):
        calls.append((services, tenant, settings))
        return client

    async def plan(self, **kwargs):
        dispatched.append((self.llm, kwargs))
        return {"action": "skip", "rationale": "Composition protocol fixture"}

    monkeypatch.setattr(model, "model_for", resolve)
    monkeypatch.setattr(cycle.LLMSkillPlanner, "__call__", plan)
    tx = SimpleNamespace(pool=SimpleNamespace(schema="fixture"))
    worker = build_role_services(role="worker", tx=tx, config=TeamEvolverConfig())
    planner = worker.extras["skill_planner"]
    assert not hasattr(planner, "llm") and not hasattr(planner, "llm_for") and calls == []
    frozen = SkillsModuleConfig(7, {"model": {"model": "frozen-model", "secret_ref": "test-secret-reference"}})
    token = set_current_tenant(TenantContext("alpha", "alpha"))
    try:
        result = await planner(config=frozen, original_keyword="preserved")
    finally:
        reset_current_tenant(token)
    assert result["action"] == "skip"
    assert [(tenant, settings) for _, tenant, settings in calls] == [("alpha", frozen.config["model"])]
    assert all(services is worker for services, _, _ in calls)
    assert dispatched == [(client, {"config": frozen, "original_keyword": "preserved"})]
