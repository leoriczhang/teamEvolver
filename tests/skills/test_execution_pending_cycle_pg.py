"""Two native shared Worker cycles retain pending Prompt edits and frozen feedback."""
from __future__ import annotations

import copy
import json
import os

import pytest

from session_ingestion.schemas import session_payload_hash
from team_replay.execution_plan import execution_plan_hash
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.cycle import LLMSkillPlanner
from team_skills.evolution.replay_case_synthesis import hydrate_cross_session_cases
from team_skills.evolution.repository import EvolutionRepository
from team_skills.execution.evolution import attach_execution_proposal
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.prompt_editor import PromptEditorLLM

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


class Payloads:
    def __init__(self):
        self.data = {}

    def get_session(self, ref):
        return copy.deepcopy(self.data[ref])


def seed(env, payloads):
    async def setup(conn):
        raw = profile_raw()
        raw["affected_skill_ids"] = []
        _, release = await ExecutionRepository(env.tx).register(
            conn, tenant_id="default", raw=raw, skill_dependencies=[], actor="test",
        )
        for index in range(1, 5):
            sid = f"source-{index}"
            ref = {"trace_id": f"trace-{index}", "source_session_key": f"root-{index}",
                   "revision": "fact-1", "context_revision": "context-1"}
            session = {"session_id": sid, "source": "langfuse", "runtime": {"type": "observed_http"},
                       "runtime_context": {"agent_id": "agent-1"}, "_summary": "A shared user correction",
                       "_judge_scores": {"source": "llm", **{key: 0.8 for key in (
                           "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage",
                       )}}, "value_judge": {"mode": "merged", "decision": "valuable", "confidence": 0.9,
                                           "reason": "Shared correction"},
                       "provenance": {"schema_version": "session-source.v1", "identity_verified": True,
                                      "trace_identity_verified": True, "source_session_key": f"root-{index}",
                                      "trace_revisions": [ref], "target_trace_refs": [ref], "context_trace_refs": []},
                       "turns": [{"turn_num": 1, "source_trace_id": f"trace-{index}",
                                  "prompt_text": "A common business task", "response_text": "Observed result",
                                  "read_skills": [], "request_context": {"evidence_level": "generation_input",
                                      "completeness": "unknown", "generation_requests": [{
                                          "observation_id": f"generation-{index}", "input": [
                                              {"role": "system", "content": "Original business Prompt"},
                                              {"role": "developer", "content": "Keep the shared business policy"},
                                              {"role": "user", "content": "A common business task"},
                                          ]}]}}]}
            payloads.data[sid] = session
            await conn.execute(
                f"INSERT INTO {env.schema}.sessions(tenant_id,session_id,user_id,decision,content_ref,content_hash) "
                "VALUES('default',$1,$2,'valuable',$1,$3)", sid, f"user-{index}", session_payload_hash(session),
            )
        return release
    return _run(env, "default", setup)


def read_candidate(env, cid):
    return _run(env, "default", lambda conn: CandidateRepository(env.tx).require(conn, cid))


def read_command(env, jid):
    return _run(env, "default", lambda conn: EvolutionRepository(env.tx).command(conn, jid))


def trigger(client, ids):
    response = client.post("/api/skill-evolution/trigger", headers=H, json={
        "allowed_modules": ["prompt"], "execution_profile_id": "instructor", "session_ids": ids,
    })
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def drive(env, services, kind):
    return env.tx.pool.run(process_once(services, owner_id="pending-cycle", kinds=[kind], max_attempts=1))


class Writer:
    def __init__(self, *, fail_first=False):
        self.requests = []
        self.fail_first = fail_first
        self.round = 1
        self.before_reply = None
        self._editor_round = None
        self.editors = []

    async def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        if self.fail_first and len(self.requests) == 1:
            raise ValueError("injected provider failure before a proposal")
        if self._editor_round != self.round:
            self._editor_round = self.round
            if self.before_reply:
                await self.before_reply()
            body = json.loads(messages[1]["content"])
            self.editors.append(PromptEditorLLM(
                [body["allowed_evidence_refs"][0]],
                replacement="Pending A" if self.round == 1 else "Pending B",
                component_id="main-prompt" if self.round == 1 else "shared-context",
            ))
        return await self.editors[-1].chat(messages, **kwargs)


def services_for(env, writer, payloads, seen_context):
    async def synthesize(**kwargs):
        seen_context.append(copy.deepcopy(kwargs["evidence_context"]))
        cases = [{"case_id": f"cross-{index}", "query": "Representative task", "skill_ids": [],
                  "checks": [{"id": f"R{n}", "kind": "output", "text": f"Condition {n}"} for n in range(6)],
                  "materials": [], "replay": {}, "metadata": {},
                  "provenance": {"session_ids": [s["session_id"] for s in kwargs["sessions"]]}}
                 for index in range(2)]
        return hydrate_cross_session_cases(cases, kwargs["sessions"], skill_name=kwargs["skill_name"])

    async def replay(job, config):
        pairs = []
        for case in job["replay_cases"]:
            provenance = case.get("provenance") or {}
            pair = {"case_id": case["case_id"], "query": case["query"], "provenance": provenance,
                    "selection": provenance.get("replay_selection")}
            for branch in ("baseline", "candidate"):
                pair[branch] = {
                    "ok": True, "completed": branch == "baseline", "execution_verification": "verified",
                    "execution_plan_hash": execution_plan_hash(job[branch + "_execution_plan"]),
                    "interactions": [{"prompt": case["query"], "response": branch + " actual response"}],
                    "checklist_report": {"items": [{**check, "satisfied": branch == "baseline",
                                                     "evidence": branch + " actual machine observation"}
                                                    for check in case["checks"]]},
                }
            pairs.append(pair)
        pairs.append({"query": "LOCKED-CANARY", "candidate": {"response": "LOCKED-CANARY",
                                                               "checklist_report": "LOCKED-CANARY"}})
        return {"verdict": "reject", "runtime_gate": {"status": "passed"},
                "efficiency": {"baseline": {"interaction_turns": 1, "tool_call_count": 0, "total_tokens": 20},
                               "candidate": {"interaction_turns": 2, "tool_call_count": 1, "total_tokens": 30}},
                "cases": pairs}

    return build_services(role="worker", tx=env.tx, handlers=register_handlers(), extras={
        "skill_planner": LLMSkillPlanner(writer), "replay_case_synthesizer": synthesize,
        "session_payloads": payloads, "skill_replay": replay,
    })


def prepare_client(client):
    _login(client, bootstrap=True)
    settings = client.get("/api/skill-evolution/settings", headers=H).json()
    changed = client.put("/api/skill-evolution/settings", headers=H,
                         json={"revision": settings["revision"], "validation": {"auto_evaluate": False}})
    assert changed.status_code == 200, changed.text


@pytest.mark.parametrize("provider_retry", [False, True])
def test_two_worker_cycles_use_pending_content_safe_feedback_and_preserve_old_commands(
    pg_env, tmp_path, monkeypatch, provider_retry,
):
    payloads, writer, seen = Payloads(), Writer(fail_first=provider_retry), []
    with _pod(pg_env, tmp_path, monkeypatch, pod="pending-prompt") as client:
        prepare_client(client)
        baseline = seed(pg_env, payloads)
        services = services_for(pg_env, writer, payloads, seen)
        first_id = trigger(client, ["source-1", "source-2"])
        drive(pg_env, services, "skill.evolve")
        if provider_retry:
            assert read_command(pg_env, first_id)["result"] is None
            retry = client.post(f"/api/jobs/{first_id}/retry", headers=H)
            assert retry.status_code == 200, retry.text
            drive(pg_env, services, "skill.evolve")
            assert writer.requests[0] == writer.requests[1]
        first = read_command(pg_env, first_id)
        cid = first["result"]["evolutions"][0]["candidate_id"]
        item = read_candidate(pg_env, cid)
        validation = client.post(f"/api/skill-candidates/{cid}/evaluate", headers=H)
        assert validation.status_code == 202, validation.text
        drive(pg_env, services, "skill.validate")
        validated = read_candidate(pg_env, cid)
        assert validated["status"] == "open" and validated["evaluation"]["replay"]["efficiency"]
        old_validation = read_command(pg_env, validation.json()["job_id"])
        writer.round = 2
        second_id = trigger(client, ["source-3", "source-4"])
        drive(pg_env, services, "skill.evolve")
        second = read_command(pg_env, second_id)
        assert second["result"]["evolutions"][0]["candidate_id"] == cid
        revised = read_candidate(pg_env, cid)
        assert revised["revision"] == item["revision"] + 1
        assert item["detail"]["editor_audit"]["planned_component_ids"] == ["main-prompt"]
        assert revised["detail"]["editor_audit"]["planned_component_ids"] == ["shared-context"]
        assert revised["detail"]["editor_audit"]["mechanical_only"] is True
        assert "business_contract_review" not in revised["detail"]["editor_audit"]
        assert (revised["detail"]["editor_audit"]["staged_hash"]
                != item["detail"]["editor_audit"]["staged_hash"])
        body = json.loads(writer.requests[-1][1]["content"])
        assert body["execution_plan"]["components"][0]["content"] == "Pending A"
        assert body["active_candidate"]["candidate_id"] == cid
        assert body["active_candidate"]["revision"] == validated["revision"]
        assert body["active_candidate"]["base_release_id"] == baseline["release_id"]
        assert body["active_candidate_feedback"]["candidate"]["total_tokens"] == 30
        feedback = body["active_candidate_replay_feedback"]
        assert feedback["candidate_revision"] == validated["revision"]
        assert len(feedback["cases"]) == 2
        for case in feedback["cases"]:
            assert case["baseline"]["checks"][0]["satisfied"] is True
            assert case["candidate"]["checks"][0]["satisfied"] is False
            assert case["candidate"]["interactions"][0]["response"] == "candidate actual response"
        assert "active_candidate_replay_feedback" not in seen[-1]
        assert "LOCKED-CANARY" not in json.dumps(writer.requests[-1])
        assert "LOCKED-CANARY" not in json.dumps(seen[-1])
        assert "working_execution_plan" not in seen[-1] and "active_candidate" not in seen[-1]
        parts = {p["component_id"]: p["content"] for p in revised["detail"]["candidate_execution_plan"]["components"]}
        assert parts == {"main-prompt": "Pending A", "shared-context": "Pending B"}
        assert revised["detail"]["component_changes"][0]["evidence_refs"] == item["detail"]["component_changes"][0][
            "evidence_refs"]
        assert revised["detail"]["base_release_id"] == baseline["release_id"]
        before, after = item["detail"]["baseline_execution_plan"], revised["detail"]["baseline_execution_plan"]
        assert {k: v for k, v in before.items() if k != "target_component_ids"} == {
            k: v for k, v in after.items() if k != "target_component_ids"
        }
        assert after["target_component_ids"] == ["main-prompt", "shared-context"]
        assert revised["detail"]["skill_dependencies"] == revised["detail"]["affected_skill_ids"] == []
        assert _run(pg_env, "default", lambda c: CandidateRepository(pg_env.tx).read_bundle(c, cid)) == {}
        assert _run(pg_env, "default", lambda c: SkillLibraryRepository(pg_env.tx).list_skills(c)) == []
        assert read_command(pg_env, first_id) == first
        assert read_command(pg_env, validation.json()["job_id"]) == old_validation


def test_concurrent_edit_discards_proposal_and_retry_does_not_rebase_its_saved_input(
    pg_env, tmp_path, monkeypatch,
):
    payloads, writer, seen = Payloads(), Writer(), []
    with _pod(pg_env, tmp_path, monkeypatch, pod="pending-race") as client:
        prepare_client(client)
        seed(pg_env, payloads)
        services = services_for(pg_env, writer, payloads, seen)
        first_id = trigger(client, ["source-1", "source-2"])
        drive(pg_env, services, "skill.evolve")
        first = read_command(pg_env, first_id)
        cid = first["result"]["evolutions"][0]["candidate_id"]
        writer.round = 2

        async def edit():
            async with pg_env.tx.tenant_tx("default") as conn:
                repo = CandidateRepository(pg_env.tx)
                item = await repo.require(conn, cid)
                await repo.revise_detail(conn, tenant_id="default", candidate_id=cid,
                                         detail={**item["detail"], "rationale": "Concurrent reviewer edit"},
                                         expected_revision=item["revision"], actor="reviewer")

        writer.before_reply = edit
        second_id = trigger(client, ["source-3", "source-4"])
        drive(pg_env, services, "skill.evolve")
        assert read_command(pg_env, second_id)["result"] is None
        revised = read_candidate(pg_env, cid)
        assert revised["revision"] == 2
        assert revised["detail"]["candidate_execution_plan"]["components"][1]["content"] != "Pending B"
        saved = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).branch(
            c, second_id, "__prompt_planning__:execution:instructor",
        ))
        assert saved["active_candidate"]["revision"] == 1
        calls = len(writer.requests)
        assert client.post(f"/api/jobs/{second_id}/retry", headers=H).status_code == 200
        drive(pg_env, services, "skill.evolve")
        assert len(writer.requests) == calls  # Stale saved input fails before another writer call.
        assert read_candidate(pg_env, cid) == revised
        assert read_command(pg_env, first_id) == first


def test_prompt_commit_locks_subject_before_reading_even_an_absent_candidate(
    pg_env, tmp_path, monkeypatch,
):
    payloads, writer, seen = Payloads(), Writer(), []
    checked = []
    original = CandidateRepository.live_for_execution

    async def inspect(repo, conn, profile_id):
        value = await original(repo, conn, profile_id)
        if writer.requests:  # The read after the writer, rather than the pre-model snapshot read.
            async with pg_env.tx.tenant_tx("default") as other:
                acquired = await other.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))",
                    "default:candidate-execution:" + profile_id,
                )
            checked.append((value, acquired))
            assert value is None and acquired is False
        return value

    monkeypatch.setattr(CandidateRepository, "live_for_execution", inspect)
    with _pod(pg_env, tmp_path, monkeypatch, pod="pending-empty-lock") as client:
        prepare_client(client)
        seed(pg_env, payloads)
        jid = trigger(client, ["source-1", "source-2"])
        drive(pg_env, services_for(pg_env, writer, payloads, seen), "skill.evolve")
        assert read_command(pg_env, jid)["result"]["candidates_queued"] == 1
        assert checked and all(value is None and acquired is False for value, acquired in checked)


def test_candidate_appearing_while_writer_runs_is_not_silently_revised(pg_env, tmp_path, monkeypatch):
    payloads, writer, seen, competing = Payloads(), Writer(), [], []
    with _pod(pg_env, tmp_path, monkeypatch, pod="pending-empty-race") as client:
        prepare_client(client)
        seed(pg_env, payloads)

        async def create_competing_candidate():
            async with pg_env.tx.tenant_tx("default") as conn:
                profile, release = await ExecutionRepository(pg_env.tx).current(conn, "instructor")
                frozen = {"candidate_kind": "execution", "execution_profile_id": "instructor",
                          "base_release_id": release["release_id"],
                          "baseline_execution_plan": release["document"]["execution_plan"],
                          "affected_skill_ids": profile["affected_skill_ids"], "skill_dependencies": []}
                detail = attach_execution_proposal(
                    {"component_changes": [component_change("Concurrent pending content")]}, {}, context=frozen,
                    allowed_modules=["prompt"], working_bundle={}, skill_changed=False,
                )["_execution_detail"]
                item = await CandidateRepository(pg_env.tx).create(
                    conn, tenant_id="default", skill_name=None, bundle={}, limits=BundleLimits(), detail=detail,
                    actor="concurrent-reviewer", candidate_id="concurrent-candidate",
                )
                competing.append(item)

        writer.before_reply = create_competing_candidate
        jid = trigger(client, ["source-1", "source-2"])
        drive(pg_env, services_for(pg_env, writer, payloads, seen), "skill.evolve")
        assert len(competing) == 1
        assert len(writer.requests) == 5
        assert len(writer.editors[0].editor_calls) == len(writer.editors[0].calls) == 5
        assert read_command(pg_env, jid)["result"] is None
        assert read_candidate(pg_env, "concurrent-candidate") == competing[0]
        assert competing[0]["revision"] == 1
        snapshot = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).branch(
            c, jid, "__prompt_planning__:execution:instructor",
        ))
        assert snapshot["active_candidate"] is None
