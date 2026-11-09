"""Tenant machine pull delivers a pinned composition and separately reported activation."""
from __future__ import annotations

import copy
import os

import pytest

from team_skills.config import BundleLimits
from team_skills.execution.client import ExecutionReleaseCache
from team_skills.execution.contracts import apply_changes
from team_skills.execution.repository import ExecutionRepository
from team_skills.library.repository import SkillLibraryRepository
from tests.support.app import _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]
AGENT = {"Authorization": "Bearer tevt_alpha"}
PATH = ("/sync/skills?user_id=agent-user&execution_profile_id=instructor"
        "&execution_capability=teamevolver.execution-plan.v1")


def setup(env, *, empty=False):
    async def seed(conn):
        library = SkillLibraryRepository(env.tx)
        dependencies = []
        if not empty:
            await library.commit_bundle(conn, tenant_id="alpha", name="demo", bundle={"SKILL.md": _skill_md("demo")},
                                        mutation_id="initial", limits=BundleLimits())
            head = await library.get_skill(conn, "demo")
            dependencies = [{"name": "demo", "version": 1, "tree_sha256": head["tree_sha256"]}]
        raw = profile_raw(user_id="agent-user")
        raw["affected_skill_ids"] = [] if empty else ["demo"]
        return await ExecutionRepository(env.tx).register(
            conn, tenant_id="alpha", raw=raw, skill_dependencies=dependencies, actor="test-reviewer",
        )
    return _run(env, "alpha", seed)


def publish(env, original):
    document = copy.deepcopy(original["document"])
    document["execution_plan"] = apply_changes(document["execution_plan"], [component_change()],
                                                 allowed_modules=["prompt"])
    return _run(env, "alpha", lambda conn: ExecutionRepository(env.tx).publish(
        conn, tenant_id="alpha", profile_id="instructor", expected_release_id=original["release_id"],
        document=document, actor="test-reviewer", candidate_id="test-candidate", candidate_revision=1,
        validation_ref="test-fixture-only",
    ))


@pytest.mark.parametrize("empty", [False, True])
def test_prompt_only_pull_invalidates_etag_and_installs_complete_pinned_release(pg_env, tmp_path, monkeypatch, empty):
    _, original = setup(pg_env, empty=empty)
    with _pod(pg_env, tmp_path, monkeypatch, pod="pull-composition") as client:
        first = client.get(PATH, headers=AGENT)
        assert first.status_code == 200, first.text
        body = first.json()["execution"]
        if empty:
            assert body["skills"] == [] and body["document"]["skill_dependencies"] == []
            assert first.json()["skills"] == []
        cache = ExecutionReleaseCache(tmp_path / "agent")
        pin = cache.install(body)
        assert pin["release_id"] == original["release_id"]
        assert client.get(PATH, headers={**AGENT, "If-None-Match": first.headers["ETag"]}).status_code == 304
        changed = publish(pg_env, original)
        response = client.get(PATH, headers={**AGENT, "If-None-Match": first.headers["ETag"]})
        assert response.status_code == 200 and response.headers["ETag"] != first.headers["ETag"]
        new = response.json()
        assert new["skills"] == first.json()["skills"]  # No Skill version changed.
        assert new["execution"]["release_id"] == changed["release_id"]
        activated = cache.install(new["execution"])
        assert activated["directory"] != pin["directory"]
        assert cache.active()["release_id"] == changed["release_id"]
        assert pin["document"]["execution_plan"]["components"][0]["content"] == "Original business Prompt"
        receipt = {"profile_id": "instructor", "release_id": changed["release_id"],
                   "composition_hash": changed["composition_hash"], "state": "activated", "request_id": "activate-1"}
        r = client.post("/sync/skills/execution-receipt?user_id=agent-user", headers=AGENT, json=receipt)
        assert r.status_code == 200 and r.json()["evidence_level"] == "agent_reported"
        assert client.post("/sync/skills/execution-receipt?user_id=agent-user", headers=AGENT,
                           json=receipt).json() == r.json()
        conflict = client.post("/sync/skills/execution-receipt?user_id=agent-user", headers=AGENT,
                               json={**receipt, "state": "downloaded"})
        assert conflict.status_code == 409
        assert client.post("/sync/skills/execution-receipt?user_id=wrong-user", headers=AGENT,
                           json={**receipt, "request_id": "other"}).status_code == 404


def test_new_skill_head_does_not_replace_a_released_composition_dependency(pg_env, tmp_path, monkeypatch):
    _, original = setup(pg_env)
    _run(pg_env, "alpha", lambda conn: SkillLibraryRepository(pg_env.tx).commit_bundle(
        conn, tenant_id="alpha", name="demo", bundle={"SKILL.md": _skill_md("demo", "Unvalidated new edit")},
        mutation_id="manual-edit", limits=BundleLimits(),
    ))
    with _pod(pg_env, tmp_path, monkeypatch, pod="pull-frozen") as client:
        response = client.get(PATH, headers=AGENT)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skills"][0]["version"] == 2
        assert body["execution"]["skills"][0]["version"] == 1
        assert body["execution"]["composition_hash"] == original["composition_hash"]
        installed = ExecutionReleaseCache(tmp_path / "agent").install(body["execution"])
        assert installed["release_id"] == original["release_id"]
        assert client.get(PATH.replace("agent-user", "another-user"), headers=AGENT).status_code == 404
        assert client.get(PATH, headers={**AGENT, "X-Tenant-Id": "default"}).status_code == 403
        assert client.get(PATH.replace("teamevolver.execution-plan.v1", "legacy"), headers=AGENT).status_code == 409
        assert client.get(PATH).status_code == 401
