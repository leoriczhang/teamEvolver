"""PostgreSQL scope acceptance keeps transient requests separate from saved policy."""
from __future__ import annotations

import os

import pytest

from team_skills.config import BundleLimits
from team_skills.evolution.repository import EvolutionRepository
from team_skills.library.repository import SkillLibraryRepository
from tests.support.app import H, _login, _pod
from tests.support.execution import profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured"),
]


def test_manual_scope_is_frozen_and_does_not_save_default(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="scope") as client:
        _login(client, bootstrap=True)
        settings = client.get("/api/skill-evolution/settings", headers=H).json()
        assert settings["evolve"]["evolution_allowed_modules"] == ["skill"]
        missing = client.post("/api/skill-evolution/trigger", headers=H, json={
            "allowed_modules": ["prompt"], "execution_profile_id": "missing-profile",
        })
        assert missing.status_code == 404, missing.text
        raw_profile = profile_raw("profile-frozen-at-acceptance")

        async def seed_skills(conn):
            for name in raw_profile["affected_skill_ids"]:
                await SkillLibraryRepository(pg_env.tx).commit_bundle(
                    conn, tenant_id="default", name=name, bundle={"SKILL.md": _skill_md(name)},
                    mutation_id="scope-seed:" + name, limits=BundleLimits(),
                )

        _run(pg_env, "default", seed_skills)
        registered = client.post("/api/skill-evolution/execution-profiles", headers=H, json=raw_profile)
        assert registered.status_code == 201, registered.text
        response = client.post("/api/skill-evolution/trigger", headers={**H, "Idempotency-Key": "scope-once"}, json={
            "allowed_modules": ["prompt"], "execution_profile_id": "profile-frozen-at-acceptance",
        })
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        frozen = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(conn, job_id))["payload"]
        assert frozen["allowed_modules"] == ["prompt"]
        assert frozen["execution_profile_id"] == "profile-frozen-at-acceptance"
        assert frozen["config_revision"] == settings["revision"]
        assert frozen["execution_baseline"]["status"] == "frozen"
        assert frozen["execution_baseline"]["release"]["release_id"] == registered.json()["current_release_id"]
        after = client.get("/api/skill-evolution/settings", headers=H).json()
        assert after == settings
        saved = client.put("/api/skill-evolution/settings", headers=H, json={
            "revision": after["revision"], "evolve": {"evolution_allowed_modules": ["skill", "prompt"]},
        })
        assert saved.status_code == 200, saved.text
        replayed = client.post("/api/skill-evolution/trigger", headers={**H, "Idempotency-Key": "scope-once"}, json={
            "allowed_modules": ["prompt"], "execution_profile_id": "profile-frozen-at-acceptance",
        })
        assert replayed.status_code == 202, replayed.text
        assert replayed.json()["job_id"] == job_id
        still_frozen = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(conn, job_id))
        still_frozen = still_frozen["payload"]
        assert still_frozen == frozen


def test_plan_scope_is_strict_and_default_is_used_for_new_commands(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="plan-scope") as client:
        _login(client, bootstrap=True)
        settings = client.get("/api/skill-evolution/settings", headers=H).json()
        for invalid in ([], None, "prompt", ["all"], ["code"], ["skill", "skill"]):
            assert client.put("/api/skill-evolution/settings", headers=H, json={
                "revision": settings["revision"], "evolve": {"evolution_allowed_modules": invalid},
            }).status_code == 400
            assert client.post("/api/skill-evolution/trigger", headers=H, json={
                "allowed_modules": invalid,
            }).status_code == 400
        saved = client.put("/api/skill-evolution/settings", headers=H, json={
            "revision": settings["revision"], "evolve": {"evolution_allowed_modules": ["prompt", "skill"]},
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["evolve"]["evolution_allowed_modules"] == ["skill", "prompt"]
        stale = client.put("/api/skill-evolution/settings", headers=H, json={
            "revision": settings["revision"], "evolve": {"evolution_allowed_modules": ["skill"]},
        })
        assert stale.status_code == 409, stale.text
        accepted = client.post("/api/skill-evolution/trigger", headers=H, json={})
        assert accepted.status_code == 202, accepted.text
        command = _run(pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(
            conn, accepted.json()["job_id"],
        ))
        assert command["payload"]["allowed_modules"] == ["skill", "prompt"]
        assert command["payload"]["config_revision"] == saved.json()["revision"]
