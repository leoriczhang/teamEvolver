"""Prompt-only evolution uses two real source roots and preserves the whole Skill."""
from __future__ import annotations

import copy
import json
import os

import pytest

from session_ingestion.schemas import session_payload_hash
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.cycle import LLMSkillPlanner
from team_skills.evolution.replay_case_synthesis import hydrate_cross_session_cases
from team_skills.evolution.repository import EvolutionRepository
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import component_change, profile_raw
from tests.support.postgres import _run
from tests.support.prompt_editor import PromptEditorLLM
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured",
)]


class Payloads:
    def __init__(self):
        self.data = {}

    def get_session(self, ref):
        return copy.deepcopy(self.data[ref])


class ProposalLLM(PromptEditorLLM):
    def __init__(self):
        super().__init__(["trace-1", "trace-2"], replacement=component_change()["content"])
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        if not self.editor_calls:
            value = json.loads(messages[1]["content"])
            assert {s["session_id"] for s in value["evidence"]} == {"source-1", "source-2"}
        return await super().chat(messages, **kwargs)


async def synthesize(**kwargs):
    values = [{"case_id": "cross-" + str(index), "query": "A representative task", "skill_ids": ["demo"],
               "checks": [{"id": "R" + str(n), "kind": "output", "text": "Condition " + str(n)} for n in range(7)],
               "materials": [], "provenance": {"session_ids": [s["session_id"] for s in kwargs["sessions"]]},
               "replay": {}, "metadata": {}} for index in range(2)]
    return hydrate_cross_session_cases(values, kwargs["sessions"], skill_name=kwargs["skill_name"])


def seed(env, payloads, *, verified=True, empty=False):
    async def setup(conn):
        old = {} if empty else {"SKILL.md": _skill_md("demo"), "scripts/task.py": b"print('existing task')\n"}
        library = SkillLibraryRepository(env.tx)
        dependencies = []
        if not empty:
            await library.commit_bundle(conn, tenant_id="default", name="demo", bundle=old,
                                        mutation_id="seed", limits=BundleLimits())
            head = await library.get_skill(conn, "demo")
            dependencies = [{"name": "demo", "version": 1, "tree_sha256": head["tree_sha256"]}]
        raw = profile_raw()
        raw["affected_skill_ids"] = [] if empty else ["demo"]
        await ExecutionRepository(env.tx).register(conn, tenant_id="default", raw=raw,
                                                  skill_dependencies=dependencies, actor="test")
        for index in (1, 2):
            sid = f"source-{index}"
            ref = {"trace_id": f"trace-{index}", "source_session_key": f"root-{index}",
                   "revision": "fact-1", "context_revision": "context-1"}
            session = {"session_id": sid, "source": "langfuse", "runtime": {"type": "observed_http"},
                       "runtime_context": {"agent_id": "agent-1"},
                       "_summary": "A user corrected a shared Prompt rule.",
                       "_judge_scores": {"source": "llm", **{key: 0.8 for key in (
                           "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage",
                       )}},
                       "value_judge": {"mode": "merged", "decision": "valuable", "confidence": 0.9,
                                       "reason": "A reusable cross-user correction"},
                       "provenance": {"schema_version": "session-source.v1", "identity_verified": verified,
                                      "trace_identity_verified": True, "source_session_key": f"root-{index}",
                                      "trace_revisions": [ref], "target_trace_refs": [ref], "context_trace_refs": []},
                       "turns": [{"turn_num": 1, "source_trace_id": f"trace-{index}",
                                  "prompt_text": "Perform a shared task", "response_text": "Old result",
                                  "read_skills": [] if empty else ["demo"], "request_context": {
                                      "evidence_level": "generation_input", "completeness": "unknown",
                                      "generation_requests": [{"observation_id": "generation-" + str(index),
                                          "input": [{"role": "system", "content": "Original business Prompt"},
                                                    {"role": "developer", "content": "Keep the shared business policy"},
                                                    {"role": "user", "content": "Perform a shared task"}]}],
                                  }}]}
            payloads.data[sid] = session
            await conn.execute(
                f"INSERT INTO {env.schema}.sessions(tenant_id,session_id,user_id,decision,content_ref,content_hash) "
                "VALUES('default',$1,$2,'valuable',$1,$3)", sid, f"user-{index}", session_payload_hash(session),
            )
        return old
    return _run(env, "default", setup)


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("publish_mode", ["manual", "automatic"])
def test_source_qualified_prompt_evolution_or_explicit_zero_candidate(
    pg_env, tmp_path, monkeypatch, verified, empty, publish_mode,
):
    payloads, llm = Payloads(), ProposalLLM()
    with _pod(pg_env, tmp_path, monkeypatch, pod="prompt-cycle") as client:
        _login(client, bootstrap=True)
        settings = client.get("/api/skill-evolution/settings", headers=H).json()
        changed = client.put("/api/skill-evolution/settings", headers=H, json={
            "revision": settings["revision"], "validation": {"publish_mode": publish_mode, "auto_evaluate": True},
        })
        assert changed.status_code == 200, changed.text
        old = seed(pg_env, payloads, verified=verified, empty=empty)
        response = client.post("/api/skill-evolution/trigger", headers=H, json={
            "allowed_modules": ["prompt"], "execution_profile_id": "instructor",
            "session_ids": ["source-1", "source-2"],
        })
        assert response.status_code == 202, response.text
        services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
            "skill_planner": LLMSkillPlanner(llm), "replay_case_synthesizer": synthesize,
            "session_payloads": payloads,
        })
        pg_env.tx.pool.run(process_once(services, owner_id="prompt-cycle", kinds=["skill.evolve"]))
        jid = response.json()["job_id"]
        command = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).command(c, jid))
        assert command["result"] is not None, command
        result = command["result"]
        expected_received = 0 if empty and not verified else 2
        assert result["received_sessions"] == expected_received
        if verified:
            assert len(llm.requests) == 5 and result["candidates_queued"] == 1
            assert len(llm.editor_calls) == len(llm.calls) == 5
            row = result["evolutions"][0]
            item = _run(pg_env, "default", lambda c: CandidateRepository(pg_env.tx).require(c, row["candidate_id"]))
            assert item["detail"]["actual_modules"] == ["prompt"]
            assert item["detail"]["allowed_modules"] == ["prompt"]
            audit = item["detail"]["editor_audit"]
            assert audit["schema_version"] == "te.prompt-editor-attempt.v1"
            assert audit["planned_component_ids"] == ["main-prompt"] and audit["applied_edits"] == 1
            assert audit["mechanical_only"] is True
            assert "business_contract_review" not in audit
            evaluation = _run(pg_env, "default", lambda c: EvolutionRepository(pg_env.tx).command(
                c, item["validate_task_id"],
            ))
            assert item["status"] == "validating" and evaluation["payload"]["mode"] == "evaluate"
            assert evaluation["kind"] == "skill.validate"  # Automatic plans still require later exact-result review.
            assert evaluation["payload"]["detail"]["editor_audit"] == audit
            bundle = _run(pg_env, "default", lambda c: CandidateRepository(pg_env.tx).read_bundle(
                c, row["candidate_id"],
            ))
            assert bundle == old and item["base_version"] == (0 if empty else 1)
            if empty:
                assert item["skill_name"] is None and item["candidate_kind"] == "execution"
                assert item["execution_profile_id"] == "instructor"
                assert row["group_key"] == "execution:instructor" and row["skill_name"] is None
                assert item["detail"]["affected_skill_ids"] == item["detail"]["skill_dependencies"] == []
                assert item["detail"]["skill_changed"] is False
                detail = client.get(f"/api/skill-candidates/{item['candidate_id']}/detail", headers=H)
                assert detail.status_code == 200, detail.text
                assert detail.json()["candidate_skill"] is None and detail.json()["candidate_skill_md"] == ""
                assert client.put(f"/api/skill-candidates/{item['candidate_id']}/content", headers=H,
                                  json={"content": "Fabricated Skill"}).status_code == 400
            assert result["effective_source_sessions"] == 2
        else:
            assert not llm.requests and result["candidates_queued"] == 0
            assert result["reason"] == ("no_verifiable_new_evidence" if expected_received else "")
            assert result["effective_source_sessions"] == 0 and result["excluded_sessions"] == expected_received
            if empty:
                assert _run(pg_env, "default", lambda c: c.fetchval(
                    f"SELECT count(*) FROM {pg_env.schema}.skill_evolution_sessions",
                )) == 0
