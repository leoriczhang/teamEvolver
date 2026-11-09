"""Worker + real PG + loopback HTTP: composition validation, publication and rollback."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from team_replay.adapters.examples.observed_http import ObservedHttpFactory
from team_replay.datasets.repository import DatasetRepository
from team_replay.datasets.schema import make_document
from team_replay.execution_plan import execution_plan_hash
from team_replay.gateway import HostReplayGateway
from team_replay.integration import FixedHost
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.history import skill_payload
from team_skills.evolution.repository import EvolutionRepository
from team_skills.evolution.validation import TrueReplayValidator
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.llm_usage import collect_llm_usage
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.replay import judge
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


def cases(skill="demo", *, family="diagnostic"):
    return [{"case_id": "task-" + family, "query": "Return the answer", "skill_ids": [skill] if skill else [],
             "checks": [{"id": "R1", "kind": "output", "text": "Return the answer"}], "materials": [],
             "provenance": {"source_runtime": {"type": "observed_http"},
                            "source_session_keys": [family]}, "replay": {}, "metadata": {}}]


def seed(env, *, joint=False, holdout=True, empty=False, strategy="enhanced-v1", shared=False):
    async def setup(conn):
        library = SkillLibraryRepository(env.tx)
        old = {} if empty else {"SKILL.md": _skill_md("demo"), "scripts/helper.py": b"print('unchanged')\n"}
        dependencies = []
        if not empty:
            await library.commit_bundle(conn, tenant_id="default", name="demo", bundle=old,
                                        mutation_id="initial", limits=BundleLimits())
            head = await library.get_skill(conn, "demo")
            dependencies = [{"name": "demo", "version": 1, "tree_sha256": head["tree_sha256"]}]
        raw = profile_raw(user_id="agent-user")
        raw["affected_skill_ids"] = [] if empty else ["demo"]
        raw["environment"]["validation"] = {"strategy": strategy}
        if shared:
            raw["components"] = raw["components"][:1]
            raw["components"][0]["content"] = "Service {{domain}}"
            raw["environment"].update(
                template_variables={"domain": "default"},
                template_input_contract={"schema_version": "teamevolver.template-input-contract.v1",
                                         "variable_names": ["domain"]},
            )
        if holdout:
            locked = make_document(dataset_id="acceptance", name="Locked acceptance", skills=[] if empty else ["demo"],
                                   cases=cases(None if empty else "demo", family="independent-new-source"),
                                   metadata={"validation_role": "locked_acceptance"})
            await DatasetRepository(env.tx).save(conn, tenant_id="default", document=locked)
            raw["environment"]["validation"].update(holdout_dataset_ids=["acceptance"])
        _, release = await ExecutionRepository(env.tx).register(
            conn, tenant_id="default", raw=raw, skill_dependencies=dependencies, actor="reviewer",
        )
        context = {"execution_profile_id": raw["profile_id"], "base_release_id": release["release_id"],
                   "baseline_execution_plan": release["document"]["execution_plan"],
                   "affected_skill_ids": raw["affected_skill_ids"], "skill_dependencies": dependencies,
                   "candidate_kind": "execution" if empty else "skill"}
        changed = {**old, "SKILL.md": _skill_md("demo", "Improved reusable steps")} if joint else old
        material = attach_execution_proposal(
            {"component_changes": [component_change("Service {{domain}}; reply concisely")
                                   if shared else component_change()]},
            skill_payload("demo", changed) or {}, context=context,
            allowed_modules=["skill", "prompt"] if joint else ["prompt"], working_bundle=old, skill_changed=joint,
        )
        with collect_llm_usage("manual-test-fixture") as meter:
            generation = meter.snapshot()  # This fixture performs no model calls.
        detail = {**material["_execution_detail"], "replay_cases": cases(None if empty else "demo"),
                  "generation_cost": generation,
                  "session_evidence": [{"session_id": "raw-1", "provenance": {"source_session_key": "diagnostic"}}]}
        if shared:
            component = release["document"]["execution_plan"]["components"][0]
            detail["replay_cases"] = []
            for domain in ("support", "operations"):
                case = cases(None, family=domain)[0]
                case["provenance"]["execution_inputs"] = {
                    "schema_version": "teamevolver.execution-inputs.v1",
                    "source_ref": {"session_id": domain, "source_session_key": domain,
                                   "trace_id": domain + "-trace", "revision": "source-r1"},
                    "template_refs": {component["component_id"]: {
                        "template_id": component["component_id"], "revision": component["revision"],
                        "content_hash": component["content_hash"]}},
                    "template_variables": {"domain": domain},
                    "input_messages": [{"role": "user", "content": "Earlier " + domain + " request"}],
                }
                detail["replay_cases"].append(case)
        candidate = await CandidateRepository(env.tx).create(
            conn, tenant_id="default", skill_name=None if empty else "demo", bundle=changed, limits=BundleLimits(),
            base_version=0 if empty else 1, proposed_action="update", detail=detail, actor="test-only",
        )
        return candidate, release, old
    return _run(env, "default", setup)


@contextmanager
def model_endpoint():
    """An actual local HTTP server; outputs test protocol facts, not model business quality."""
    sent = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            sent.append(body)
            improved = body["messages"][0]["content"] == "Improved business Prompt"
            result = {"choices": [{"message": {"content": "Answer"}}],
                      "usage": {"total_tokens": 10 if improved else 20}}
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port) + "/chat", sent
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def worker(env, endpoint=None, replay=None):
    if replay is None:
        factory = ObservedHttpFactory({"endpoint": endpoint})
        validator = TrueReplayValidator(HostReplayGateway(FixedHost(factory, {}), judge))

        async def replay(job, config):
            return await validator(job, {"concurrency": 2, "timeout_seconds": 30, "max_interactions": 1})
    return build_services(role="worker", tx=env.tx, handlers=register_handlers(), extras={"skill_replay": replay})


def drive(env, services):
    return env.tx.pool.run(process_once(services, owner_id="execution-worker"))


def candidate(env, cid):
    return _run(env, "default", lambda conn: CandidateRepository(env.tx).require(conn, cid))


def command(env, jid):
    return _run(env, "default", lambda conn: EvolutionRepository(env.tx).command(conn, jid))


def attest(client, cid, revision=1):
    frozen = client.get(f"/api/skill-candidates/{cid}/detail", headers=H).json()["current_case_ids"]
    response = client.post(f"/api/skill-candidates/{cid}/independent-validation", headers=H, json={
        "candidate_revision": revision, "status": "passed", "method": "programmatic",
        "evidence_ref": "test-only:loopback-output-check", "case_ids": frozen,
        "notes": "Protocol-only test oracle. No claim of model business improvement.",
    })
    assert response.status_code == 200, response.text


def test_shared_template_worker_freezes_and_observes_each_case_inputs_without_publishing(
    pg_env, tmp_path, monkeypatch,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="shared-inputs") as client, model_endpoint() as (endpoint, sent):
        _login(client, bootstrap=True)
        item, release, _ = seed(pg_env, empty=True, holdout=False, strategy="standard-v1", shared=True)
        response = client.post(f"/api/skill-candidates/{item['candidate_id']}/evaluate", headers=H)
        assert response.status_code == 202, response.text
        drive(pg_env, worker(pg_env, endpoint))
        saved = command(pg_env, response.json()["job_id"])
        evaluation = saved["result"]["evaluation"]
        assert evaluation["execution_gate"]["status"] == "passed", json.dumps(evaluation, indent=2)
        assert set(saved["payload"]["detail"]["validation_plan"]["case_plan_hashes"]) == {
            "task-support", "task-operations",
        }
        assert len(sent) == 4
        for domain in ("support", "operations"):
            requests = [body for body in sent if body["messages"][0]["content"].startswith("Service " + domain)]
            assert len(requests) == 2
            assert all(body["messages"][1:] == [
                {"role": "user", "content": "Earlier " + domain + " request"},
                {"role": "user", "content": "Return the answer"},
            ] for body in requests)
        current = candidate(pg_env, item["candidate_id"])
        assert current["status"] == "open"
        profile, unchanged = _run(pg_env, "default", lambda conn: ExecutionRepository(pg_env.tx).current(
            conn, "instructor",
        ))
        assert profile["current_release_id"] == release["release_id"]
        assert unchanged["document"] == release["document"]


@pytest.mark.parametrize("joint,empty,maximum", [
    (False, False, 1500000), (True, False, 1500000), (False, True, 1500000), (False, True, None),
])
def test_loopback_observed_worker_publishes_one_composition_and_rolls_back_atomically(
    pg_env, tmp_path, monkeypatch, joint, empty, maximum,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="execution-pipeline") as client, model_endpoint() as (endpoint, sent):
        _login(client, bootstrap=True)
        item, original, old = seed(pg_env, joint=joint, empty=empty)
        cid = item["candidate_id"]
        services = worker(pg_env, endpoint)
        revision = item["revision"]
        if empty:
            budget = client.post(f"/api/skill-candidates/{cid}/validation-budget", headers=H, json={
                "candidate_revision": 1, "max_total_tokens": maximum,
                "reason": "Explicit isolated test authority; original profile environment remains unchanged",
            })
            assert budget.status_code == 200, budget.text
            revision = budget.json()["candidate_revision"]
        first = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H)
        assert first.status_code == 202, first.text
        drive(pg_env, services)
        first_result = command(pg_env, first.json()["job_id"])["result"]
        assert first_result["evaluation"]["execution_gate"]["status"] == "passed", json.dumps(first_result, indent=2)
        if empty:
            frozen = command(pg_env, first.json()["job_id"])["payload"]["detail"]
            dataset = _run(pg_env, "default", lambda conn: DatasetRepository(pg_env.tx).get(
                conn, frozen["replay_sampling"]["current_dataset_id"],
            ))
            assert dataset["skills"] == [] and all(case["skill_ids"] == [] for case in dataset["cases"])
            assert dataset["metadata"]["execution_profile_id"] == "instructor"
            assert dataset["metadata"]["baseline_execution_plan_hash"] == execution_plan_hash(
                frozen["baseline_execution_plan"],
            )
            assert all(case["skill_ids"] == [] for case in frozen["replay_cases"])
            assert frozen["validation_budget"]["max_total_tokens"] == maximum
            assert frozen["validation_plan"]["probe_policy"]["max_total_tokens"] == maximum
            assert frozen["validation_plan"]["holdout"]["policy"]["max_total_tokens"] == maximum
            assert first_result["evaluation"]["cost_gate"]["budget_tokens"] == maximum
            assert frozen["baseline_execution_plan"]["environment"]["validation"].get("max_total_tokens", 1000000) \
                == 1000000
        attest(client, cid, revision=revision)
        calls_before_publish = len(sent)
        response = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "auto"})
        assert response.status_code == 202, response.text
        drive(pg_env, services)
        result = command(pg_env, response.json()["job_id"])["result"]
        assert result["status"] == "published", json.dumps(result, indent=2)
        assert len(sent) == calls_before_publish  # Publication never creates unreviewed stochastic outputs.
        publication = command(pg_env, response.json()["job_id"])
        assert publication["kind"] == "skill.evolve"
        assert publication["payload"]["operation"] == "execution_publish_reviewed"
        assert result["validation_run_id"] == first.json()["job_id"]
        assert command(pg_env, first.json()["job_id"])["result"] == first_result
        saved_replay = _run(pg_env, "default", lambda conn: ReplayRepository(pg_env.tx).get(
            conn, "skill", first.json()["job_id"],
        ))
        assert saved_replay["result"] == first_result["evaluation"]
        row = candidate(pg_env, cid)
        assert row["published_version"] == (2 if joint else 0)  # Existing ledger uses zero for no Skill commit.
        replay = row["evaluation"]["replay"]
        assert replay["execution_gate"]["status"] == "passed"
        assert replay["probe_gate"]["status"] == "passed"
        assert replay["cost_gate"]["status"] == "passed"
        assert replay["cost_gate"]["prior_validation_runs"] == 0
        assert replay["cost_gate"]["known_total_tokens"] == sum(
            10 if body["messages"][0]["content"] == "Improved business Prompt" else 20 for body in sent
        )
        assert {r["kind"] for r in replay["probe_gate"]["runs"]} >= {"aa", "repeat-1"}
        assert len(sent) >= 8  # Two real branches, locked Case, A/A and repeat.
        assert all(body["messages"][1]["content"] == "Keep the shared business policy" for body in sent)
        repo = ExecutionRepository(pg_env.tx)
        _, released = _run(pg_env, "default", lambda conn: repo.current(conn, "instructor"))
        assert released["release_id"] == result["release_id"]
        assert released["validation_ref"] == first.json()["job_id"]
        head = _run(pg_env, "default", lambda conn: SkillLibraryRepository(pg_env.tx).get_skill(conn, "demo"))
        assert head is None if empty else head["version"] == (2 if joint else 1)
        if not joint and not empty:
            assert _run(pg_env, "default", lambda conn: SkillLibraryRepository(pg_env.tx).read_version_bundle(
                conn, "demo", 1,
            )) == old
        rollback = client.post("/api/skill-evolution/execution-profiles/instructor/rollback", headers=H, json={
            "release_id": original["release_id"], "expected_release_id": released["release_id"],
        })
        assert rollback.status_code == 202, rollback.text
        drive(pg_env, services)
        assert command(pg_env, rollback.json()["job_id"])["result"]["status"] == "rolled_back"
        _, restored = _run(pg_env, "default", lambda conn: repo.current(conn, "instructor"))
        assert restored["restored_from"] == original["release_id"]
        assert restored["document"]["execution_plan"]["components"][0]["content"] == "Original business Prompt"
        if empty:
            assert restored["document"]["skill_dependencies"] == []
            assert item["skill_name"] is None and item["candidate_kind"] == "execution"
            assert _run(pg_env, "default", lambda conn: CandidateRepository(pg_env.tx).read_bundle(conn, cid)) == {}
            assert _run(pg_env, "default", lambda conn: SkillLibraryRepository(pg_env.tx).list_skills(conn)) == []
        else:
            dependency = restored["document"]["skill_dependencies"][0]
            assert dependency["version"] == (3 if joint else 1)
            assert _run(pg_env, "default", lambda conn: SkillLibraryRepository(pg_env.tx).read_version_bundle(
                conn, "demo", dependency["version"],
            )) == old


@pytest.mark.parametrize("empty", [False, True])
def test_self_reported_loading_and_missing_acceptance_never_publish_even_force(pg_env, tmp_path, monkeypatch, empty):
    async def declared(job, config):
        return {"verdict": "accept", "runtime_gate": {"status": "passed"},
                "efficiency": {"baseline": {"total_tokens": 20}, "candidate": {"total_tokens": 10}}}
    with _pod(pg_env, tmp_path, monkeypatch, pod="false-receipt") as client:
        _login(client, bootstrap=True)
        item, _, _ = seed(pg_env, holdout=False, empty=empty)
        cid = item["candidate_id"]
        first = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H)
        assert first.status_code == 202, first.text
        drive(pg_env, worker(pg_env, replay=declared))
        attest(client, cid)
        response = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "force"})
        assert response.status_code == 409, response.text
        assert "execution_not_observed" in response.text and "locked_acceptance_missing" in response.text
        assert candidate(pg_env, cid)["status"] == "open"


@pytest.mark.parametrize("joint,empty", [(False, False), (True, False), (False, True)])
def test_standard_main_replay_can_be_published_without_holdout_probes_or_independent_declaration(
    pg_env, tmp_path, monkeypatch, joint, empty,
):
    with _pod(pg_env, tmp_path, monkeypatch, pod="standard-execution") as client, model_endpoint() as (endpoint, sent):
        _login(client, bootstrap=True)
        item, original, _ = seed(pg_env, joint=joint, empty=empty, holdout=False, strategy="standard-v1")
        cid = item["candidate_id"]
        services = worker(pg_env, endpoint)
        response = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H)
        assert response.status_code == 202, response.text
        drive(pg_env, services)
        run = command(pg_env, response.json()["job_id"])
        frozen = run["payload"]["detail"]
        evaluation = run["result"]["evaluation"]
        assert frozen["validation_plan"]["validation_strategy"] == "standard-v1"
        assert frozen["validation_plan"]["independent_verification_required"] is False
        assert frozen["validation_plan"]["holdout"]["status"] == "not_required"
        assert frozen["validation_plan"]["probe_policy"]["max_total_tokens"] is None
        assert frozen["candidate_execution_plan"]["environment"]["validation"] == {"strategy": "standard-v1"}
        assert not any(case["provenance"].get("replay_selection") == "locked_acceptance"
                       for case in frozen["replay_cases"])
        assert evaluation["validation_verdict"] == evaluation["replay"]["verdict"] == "accept"
        assert evaluation["execution_gate"]["status"] == "passed"
        assert evaluation["replay"]["probe_gate"] == {"status": "not_required"}
        assert evaluation["replay"]["cost_gate"]["budget_tokens"] is None
        assert evaluation["publication_gate"]["passed"] is True
        assert sent
        calls = len(sent)
        publish = client.post(f"/api/skill-candidates/{cid}/validate", headers=H, json={"mode": "auto"})
        assert publish.status_code == 202, publish.text
        drive(pg_env, services)
        result = command(pg_env, publish.json()["job_id"])["result"]
        assert result["status"] == "published", json.dumps(result, indent=2)
        assert result["validation_run_id"] == run["command_id"]
        assert len(sent) == calls  # No new stochastic run and no independent-validation declaration.
        _, release = _run(pg_env, "default", lambda conn: ExecutionRepository(pg_env.tx).current(conn, "instructor"))
        assert release["release_id"] != original["release_id"]
        assert release["validation_ref"] == run["command_id"]
