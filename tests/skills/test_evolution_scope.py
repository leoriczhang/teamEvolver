"""Mutation scope is explicit, frozen and compatible with legacy commands."""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from team_skills.api import candidates, prompts
from team_skills.config import replay_case_counts
from team_skills.evolution import schedule
from team_skills.evolution.scope import (
    configured_allowed_modules,
    normalize_allowed_modules,
    normalize_execution_profile_id,
    resolved_allowed_modules,
)


@pytest.mark.parametrize("selection", [None, [], "skill", {}, ["tool"], ["all"], [True], ["skill", "skill"]])
def test_explicit_scope_rejects_empty_unknown_and_duplicate_modules(selection):
    with pytest.raises(ValueError, match="allowed_modules"):
        normalize_allowed_modules(selection)


def test_legacy_commands_do_not_inherit_new_plan_scope():
    payload = {"config": {"evolution_allowed_modules": ["skill", "prompt"]}}
    assert resolved_allowed_modules(payload) == ["skill"]
    assert configured_allowed_modules({}) == ["skill"]
    assert normalize_allowed_modules(["prompt", "skill"]) == ["skill", "prompt"]
    frozen = resolved_allowed_modules({"allowed_modules": ["prompt"]})
    assert frozen == ["prompt"]


def test_legacy_sampling_counts_keep_the_existing_two_and_three_defaults():
    assert replay_case_counts({}) == {"current": 2, "historical": 3}


@pytest.mark.parametrize("key,value", [
    ("evolution_current_case_count", 0), ("evolution_current_case_count", 7),
    ("evolution_current_case_count", True), ("evolution_current_case_count", "3"),
    ("evolution_historical_case_count", 0), ("evolution_historical_case_count", 31),
    ("evolution_historical_case_count", None), ("evolution_historical_case_count", 6.0),
])
def test_sampling_counts_reject_invalid_saved_values(key, value):
    with pytest.raises(ValueError, match=key):
        replay_case_counts({key: value})


@pytest.mark.parametrize("value", [None, 12, " padded ", "\x00", "p" * 201])
def test_execution_profile_identity_is_not_coerced(value):
    with pytest.raises(ValueError, match="execution_profile_id"):
        normalize_execution_profile_id(value)


@pytest.fixture
def acceptance(monkeypatch):
    config = {"evolution_trigger_mode": "automatic", "evolution_allowed_modules": ["skill", "prompt"]}
    row = SimpleNamespace(revision=7, config=config)
    configs = SimpleNamespace(read_tenant_module=AsyncMock(return_value=row), write_tenant_module=AsyncMock())
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    conn = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value={"sessions": 4, "users": 2}))

    async def fetchval(sql, *args):
        return now if "clock_timestamp" in sql else None

    conn.fetchval = AsyncMock(side_effect=fetchval)

    @asynccontextmanager
    async def tenant_tx(_tenant):
        yield conn

    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=tenant_tx, pool=SimpleNamespace(schema="test")),
        extras={"config_repo": configs},
    )
    queued = []

    async def enqueue(*args, **kwargs):
        queued.append(copy.deepcopy(kwargs))
        return {"command_id": kwargs["command_id"]}, True

    for module in (candidates, prompts):
        monkeypatch.setattr(module, "actor", lambda request: "reviewer")
        monkeypatch.setattr(module, "current_tenant_id", lambda: "default")
    monkeypatch.setattr(candidates, "enqueue_command", enqueue)
    monkeypatch.setattr(schedule, "enqueue_command", enqueue)
    monkeypatch.setattr(candidates, "freeze_profile_scope", AsyncMock(return_value={}))
    monkeypatch.setattr(schedule, "freeze_profile_scope", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(candidates.create_router(services))
    app.include_router(prompts.create_router(services))
    return SimpleNamespace(app=app, services=services, queued=queued, configs=configs, row=row)


@pytest.mark.asyncio
async def test_manual_override_freezes_scope_without_saving_plan(acceptance):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        response = await client.post("/api/skill-evolution/trigger", json={
            "allowed_modules": ["prompt"], "execution_profile_id": "profile-one",
        })
        assert response.status_code == 202, response.text
        frozen = acceptance.queued[0]["payload"]
        assert frozen["allowed_modules"] == ["prompt"]
        assert frozen["config_revision"] == 7
        assert frozen["execution_profile_id"] == "profile-one"
        assert acceptance.row.config["evolution_allowed_modules"] == ["skill", "prompt"]
        acceptance.configs.write_tenant_module.assert_not_awaited()
        acceptance.row.config["evolution_allowed_modules"] = ["skill"]
        assert frozen["allowed_modules"] == ["prompt"]


@pytest.mark.asyncio
async def test_omitted_scope_uses_plan_and_old_plan_stays_skill_only(acceptance):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        assert (await client.post("/api/skill-evolution/trigger", json={})).status_code == 202
        assert acceptance.queued[-1]["payload"]["allowed_modules"] == ["skill", "prompt"]
        acceptance.row.config.pop("evolution_allowed_modules")
        assert (await client.post("/api/skill-evolution/trigger", json={})).status_code == 202
        assert acceptance.queued[-1]["payload"]["allowed_modules"] == ["skill"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [[], None, "prompt", ["code"], ["skill", "skill"]])
async def test_invalid_manual_scope_is_not_enqueued(acceptance, selection):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        response = await client.post("/api/skill-evolution/trigger", json={"allowed_modules": selection})
        assert response.status_code == 400, response.text
        assert not acceptance.queued


@pytest.mark.asyncio
async def test_plan_scope_is_validated_and_revision_is_retained(acceptance):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        response = await client.get("/api/skill-evolution/settings")
        assert response.json()["evolve"]["evolution_allowed_modules"] == ["skill", "prompt"]
        for selection in ([], ["tool"], ["all"], None):
            response = await client.put("/api/skill-evolution/settings", json={
                "revision": 7, "evolve": {"evolution_allowed_modules": selection},
            })
            assert response.status_code == 400, response.text
        acceptance.configs.write_tenant_module.assert_not_awaited()
        response = await client.put("/api/skill-evolution/settings", json={
            "revision": 7, "evolve": {"evolution_allowed_modules": ["prompt", "skill"]},
        })
        assert response.status_code == 200, response.text
        saved = acceptance.configs.write_tenant_module.call_args.kwargs
        assert saved["expected_revision"] == 7
        assert saved["config"]["evolution_allowed_modules"] == ["skill", "prompt"]


@pytest.mark.asyncio
async def test_sampling_settings_are_validated_and_frozen_for_new_commands(acceptance):
    acceptance.row.config.update(evolution_current_case_count=3, evolution_historical_case_count=6)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        settings = (await client.get("/api/skill-evolution/settings")).json()
        assert settings["evolve"]["evolution_current_case_count"] == 3
        assert settings["evolve"]["evolution_historical_case_count"] == 6
        for key, invalid in (("evolution_current_case_count", 7), ("evolution_historical_case_count", True)):
            assert (await client.put("/api/skill-evolution/settings", json={
                "revision": 7, "evolve": {key: invalid},
            })).status_code == 400
        acceptance.configs.write_tenant_module.assert_not_awaited()
        response = await client.put("/api/skill-evolution/settings", json={
            "revision": 7, "evolve": {"evolution_current_case_count": 3, "evolution_historical_case_count": 6},
        })
        assert response.status_code == 200
        assert acceptance.configs.write_tenant_module.call_args.kwargs["expected_revision"] == 7
        assert (await client.post("/api/skill-evolution/trigger", json={})).status_code == 202
        frozen = acceptance.queued[-1]["payload"]
        acceptance.row.config.update(evolution_current_case_count=2, evolution_historical_case_count=3)
        assert replay_case_counts(frozen["config"]) == {"current": 3, "historical": 6}


@pytest.mark.asyncio
async def test_automatic_command_freezes_scope_and_config_revision(acceptance):
    outcome = await schedule.evolution_tick(acceptance.services, tenant_id="default")
    assert outcome["state"] == "enqueued"
    payload = acceptance.queued[-1]["payload"]
    assert payload["allowed_modules"] == ["skill", "prompt"]
    assert payload["config_revision"] == 7
    assert payload["execution_profile_id"] == ""
    acceptance.row.config["evolution_allowed_modules"] = ["skill"]
    assert resolved_allowed_modules(payload) == ["skill", "prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [[], None, ["all"]])
async def test_invalid_saved_scope_fails_closed_instead_of_expanding(acceptance, invalid):
    acceptance.row.config["evolution_allowed_modules"] = invalid
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=acceptance.app), base_url="http://test") as client:
        assert (await client.get("/api/skill-evolution/settings")).status_code == 503
        assert (await client.post("/api/skill-evolution/trigger", json={})).status_code == 503
    assert not acceptance.queued
