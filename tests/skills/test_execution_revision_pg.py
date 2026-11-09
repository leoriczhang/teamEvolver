"""Candidate edits and attestations bind the actual revision under concurrent PG work."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.repository import EvolutionRepository, lock_key
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.runtime import build_services, process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"),
        reason="isolated PostgreSQL not configured",
    ),
]


def seed(env, *, allowed=("skill", "prompt")):
    async def setup(conn):
        original = {"SKILL.md": _skill_md("demo"), "scripts/helper.py": b"print('frozen')\n"}
        library = SkillLibraryRepository(env.tx)
        await library.commit_bundle(
            conn, tenant_id="default", name="demo", bundle=original, mutation_id="seed", limits=BundleLimits()
        )
        head = await library.get_skill(conn, "demo")
        dependencies = [{"name": "demo", "version": 1, "tree_sha256": head["tree_sha256"]}]
        raw = profile_raw()
        raw["affected_skill_ids"] = ["demo"]
        _, release = await ExecutionRepository(env.tx).register(
            conn,
            tenant_id="default",
            raw=raw,
            skill_dependencies=dependencies,
            actor="reviewer",
        )
        context = {
            "execution_profile_id": raw["profile_id"],
            "base_release_id": release["release_id"],
            "baseline_execution_plan": release["document"]["execution_plan"],
            "affected_skill_ids": ["demo"],
            "skill_dependencies": dependencies,
        }
        proposal = attach_execution_proposal(
            {"component_changes": [component_change()]},
            {},
            context=context,
            allowed_modules=list(allowed),
            working_bundle=original,
            skill_changed=False,
        )
        detail = {
            **proposal["_execution_detail"],
            "session_evidence": [{"provenance": {"source_session_key": "dev"}}],
            "replay_cases": [
                {
                    "case_id": "case-one",
                    "query": "Return answer",
                    "skill_ids": ["demo"],
                    "checks": [{"id": "one", "kind": "output", "text": "Answer"}],
                    "provenance": {"source_runtime": {"type": "observed_http"}, "source_session_key": "dev"},
                }
            ],
        }
        candidate = await CandidateRepository(env.tx).create(
            conn,
            tenant_id="default",
            skill_name="demo",
            bundle=original,
            limits=BundleLimits(),
            base_version=1,
            detail=detail,
            proposed_action="update",
            actor="independent-test",
        )
        return candidate, original

    return _run(env, "default", setup)


def current(env, identity):
    return _run(env, "default", lambda conn: CandidateRepository(env.tx).require(conn, identity))


def assert_scope(item, changed):
    detail = item["detail"]
    assert detail["skill_changed"] is changed
    assert detail["actual_modules"] == (["skill", "prompt"] if changed else ["prompt"])
    for key in ("baseline_execution_plan", "candidate_execution_plan"):
        assert detail[key]["binding"]["allow_skill_change"] is changed
        assert detail[key]["target_component_ids"] == ["main-prompt"]


def test_prompt_only_candidate_rejects_manual_skill_edit_without_touching_revision_or_bytes(
    pg_env, tmp_path, monkeypatch
):
    item, original = seed(pg_env, allowed=("prompt",))
    with _pod(pg_env, tmp_path, monkeypatch, pod="revision-scope") as client:
        _login(client, bootstrap=True)
        response = client.put(
            f"/api/skill-candidates/{item['candidate_id']}/content",
            headers=H,
            json={"content": "Unauthorised changed Skill", "candidate_revision": 1},
        )
        assert response.status_code == 400 and "scope_violation" in response.text
    saved = current(pg_env, item["candidate_id"])
    assert saved["revision"] == 1 and saved["tree_sha256"] == item["tree_sha256"]
    bundle = _run(
        pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).read_bundle(conn, item["candidate_id"])
    )
    assert bundle == original


def test_joint_allowed_candidate_recomputes_actual_scope_after_skill_edit_and_exact_restoration(pg_env):
    item, original = seed(pg_env)
    assert_scope(item, False)
    repo = CandidateRepository(pg_env.tx)
    changed = {**original, "SKILL.md": _skill_md("demo", "Changed reusable steps")}
    revised = _run(
        pg_env,
        "default",
        lambda conn: repo.revise_content(
            conn,
            tenant_id="default",
            candidate_id=item["candidate_id"],
            bundle=changed,
            limits=BundleLimits(),
            actor="reviewer",
            expected_revision=1,
        ),
    )
    assert_scope(revised, True)
    assert revised["revision"] == 2
    restored = _run(
        pg_env,
        "default",
        lambda conn: repo.revise_content(
            conn,
            tenant_id="default",
            candidate_id=item["candidate_id"],
            bundle=original,
            limits=BundleLimits(),
            actor="reviewer",
            expected_revision=2,
        ),
    )
    assert_scope(restored, False)
    assert restored["revision"] == 3 and restored["tree_sha256"] == item["tree_sha256"]


def test_validation_acceptance_recomputes_scope_from_frozen_bytes_not_stale_detail(pg_env, tmp_path, monkeypatch):
    item, original = seed(pg_env)
    changed = {**original, "SKILL.md": _skill_md("demo", "Changed reusable steps")}
    revised = _run(
        pg_env,
        "default",
        lambda conn: CandidateRepository(pg_env.tx).revise_content(
            conn,
            tenant_id="default",
            candidate_id=item["candidate_id"],
            bundle=changed,
            limits=BundleLimits(),
            actor="reviewer",
            expected_revision=1,
        ),
    )
    # Represent a persisted old-version detail, before scope reconciliation existed.
    _run(
        pg_env,
        "default",
        lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.skill_candidates SET detail=$2::jsonb WHERE candidate_id=$1",
            item["candidate_id"],
            json.dumps(item["detail"]),
        ),
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="revision-accept") as client:
        _login(client, bootstrap=True)
        response = client.post(f"/api/skill-candidates/{item['candidate_id']}/evaluate", headers=H)
        assert response.status_code == 202, response.text
    command = _run(
        pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(conn, response.json()["job_id"])
    )
    assert command["payload"]["revision"] == revised["revision"]
    assert_scope({"detail": command["payload"]["detail"]}, True)


def test_independent_declaration_requires_evaluation_all_frozen_cases_and_current_revision(
    pg_env, tmp_path, monkeypatch
):
    item, _ = seed(pg_env)
    identity = item["candidate_id"]
    declaration = {
        "candidate_revision": 1,
        "status": "passed",
        "method": "domain_review",
        "evidence_ref": "test:independent-review",
        "case_ids": ["case-one"],
        "notes": "Test metadata only",
    }

    async def scripted_replay(job, config):
        return {
            "verdict": "inconclusive",
            "runtime_gate": {"status": "inconclusive"},
            "cases": [],
            "efficiency": {"baseline": {"total_tokens": 1}, "candidate": {"total_tokens": 1}},
        }

    services = build_services(
        role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={"skill_replay": scripted_replay}
    )
    with _pod(pg_env, tmp_path, monkeypatch, pod="revision-attest") as client:
        _login(client, bootstrap=True)
        path = f"/api/skill-candidates/{identity}/independent-validation"
        assert client.post(path, headers=H, json=declaration).status_code == 409
        evaluation = client.post(f"/api/skill-candidates/{identity}/evaluate", headers=H)
        assert evaluation.status_code == 202, evaluation.text
        pg_env.tx.pool.run(process_once(services, owner_id="isolated-scripted-validator", tenants=("default",)))
        command = _run(
            pg_env, "default", lambda conn: EvolutionRepository(pg_env.tx).command(conn, evaluation.json()["job_id"])
        )
        cases = [case["case_id"] for case in command["payload"]["detail"]["replay_cases"]]
        assert client.post(path, headers=H, json={**declaration, "candidate_revision": 2}).status_code == 409
        assert client.post(path, headers=H, json={**declaration, "case_ids": ["invented"]}).status_code == 400
        assert client.post(path, headers=H, json={**declaration, "validation_input_hash": "forged"}).status_code == 400
        response = client.post(path, headers=H, json={**declaration, "case_ids": cases})
        assert response.status_code == 200, response.text
        saved = current(pg_env, identity)["detail"]["independent_validation"]
        assert saved["validation_run_id"] == evaluation.json()["job_id"]
        assert saved["schema_version"] == "teamevolver.independent-validation.v2"
        assert saved["validation_payload_hash"] == command["payload_hash"]
        from team_replay._util import stable_hash
        assert saved["validation_result_hash"] == stable_hash(command["result"]["evaluation"])
        assert saved["validation_input_hash"] == command["payload"]["detail"]["validation_plan"]["input_hash"]
        changed = client.put(
            f"/api/skill-candidates/{identity}/execution-content",
            headers=H,
            json={
                "candidate_revision": 1,
                "component_changes": [component_change("New human Prompt")],
            },
        )
        assert changed.status_code == 200, changed.text
        assert "independent_validation" not in current(pg_env, identity)["detail"]
        assert client.post(path, headers=H, json=declaration).status_code == 409


def test_prompt_edit_and_new_evolution_create_do_not_reverse_pg_lock_order(pg_env, tmp_path, monkeypatch):
    item, original = seed(pg_env)
    identity = item["candidate_id"]
    row_locked, create_locked, guard = threading.Event(), threading.Event(), threading.Lock()
    real_require = CandidateRepository.require

    async def gated_require(self, conn, candidate_id, *, for_update=False):
        value = await real_require(self, conn, candidate_id, for_update=for_update)
        with guard:
            should_gate = for_update and candidate_id == identity and not row_locked.is_set()
            if should_gate:
                row_locked.set()
        if should_gate:
            assert await asyncio.to_thread(create_locked.wait, 5), "creator never held the advisory lock"
        return value

    monkeypatch.setattr(CandidateRepository, "require", gated_require)

    async def create(conn):
        await conn.execute("SET LOCAL statement_timeout='5s'")
        await lock_key(conn, "default", "candidate-skill:demo")
        create_locked.set()
        return await CandidateRepository(pg_env.tx).create(
            conn,
            tenant_id="default",
            skill_name="demo",
            bundle=original,
            limits=BundleLimits(),
            base_version=1,
            proposed_action="update",
            detail=copy.deepcopy(item["detail"]),
            actor="concurrent-evolution",
        )

    with _pod(pg_env, tmp_path, monkeypatch, pod="revision-race") as client:
        _login(client, bootstrap=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            edited = executor.submit(
                client.put,
                f"/api/skill-candidates/{identity}/execution-content",
                headers=H,
                json={"candidate_revision": 1, "component_changes": [component_change("Human Prompt")]},
            )
            assert row_locked.wait(5), "API did not reach the locked Candidate"
            created = executor.submit(_run, pg_env, "default", create)
            response, result = edited.result(timeout=8), created.result(timeout=8)
        assert response.status_code == 200, response.text
        assert result["candidate_id"] == identity and result["revision"] == 3
    assert current(pg_env, identity)["revision"] == 3
