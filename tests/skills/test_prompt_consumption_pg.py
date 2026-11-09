"""One archived Session can supply distinct registered Prompt profiles once each."""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from session_ingestion.schemas import session_payload_hash
from team_skills.evolution.cycle import _inputs
from team_skills.evolution.repository import EvolutionRepository
from team_skills.execution.repository import ExecutionRepository
from team_skills.handlers import register_handlers
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from tests.support.app import H, _login, _pod
from tests.support.execution import profile_raw
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def frozen(profile_id):
    return {"execution_profile_id": profile_id, "allowed_modules": ["prompt"], "execution_baseline": {
        "profile": {"profile_id": profile_id, "affected_skill_ids": []}}}


def test_profile_consumption_is_independent_and_legacy_completed_prompt_commands_are_recognized(pg_env):
    repo = EvolutionRepository(pg_env.tx)
    sessions = [{"session_id": "same-session", "content_hash": "same-hash"}]

    async def exercise(conn):
        await repo.consume(conn, tenant_id="alpha", command_id="new-one", sessions=sessions, scope="execution:p1")
        assert await repo.consumed(conn, sessions, scope="execution:p1") == {("same-session", "same-hash")}
        assert await repo.consumed(conn, sessions, scope="execution:p2") == set()
        assert await repo.consumed(conn, sessions) == set()  # Skill scope is unchanged.
        await repo.consume(conn, tenant_id="alpha", command_id="new-two", sessions=sessions, scope="execution:p2")
        await repo.consume(conn, tenant_id="alpha", command_id="repeat", sessions=sessions, scope="execution:p1")
        assert await conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.skill_evolution_sessions") == 2
        historical = [{"session_id": "historical", "content_hash": "old-hash"}]
        await repo.accept(conn, tenant_id="alpha", command_id="old-p1", kind="skill.evolve",
                          request={}, payload=frozen("p1"))
        await repo.finish(conn, "old-p1", {"status": "completed"})
        await repo.consume(conn, tenant_id="alpha", command_id="old-p1", sessions=historical)
        assert await repo.consumed(conn, historical, scope="execution:p1") == {("historical", "old-hash")}
        assert await repo.consumed(conn, historical, scope="execution:p2") == set()
        assert await repo.consumed(conn, historical) == {("historical", "old-hash")}
        # Unrelated legacy Skill processing cannot suppress a registered Prompt.
        await repo.accept(conn, tenant_id="alpha", command_id="old-skill", kind="skill.evolve",
                          request={}, payload={"allowed_modules": ["skill"]})
        await repo.finish(conn, "old-skill", {"status": "completed"})
        legacy = [{"session_id": "skill-only", "content_hash": "skill-hash"}]
        await repo.consume(conn, tenant_id="alpha", command_id="old-skill", sessions=legacy)
        assert await repo.consumed(conn, legacy, scope="execution:p1") == set()
        assert await repo.consumed(conn, legacy) == {("skill-only", "skill-hash")}

    _run(pg_env, "alpha", exercise)
    assert _run(pg_env, "beta", lambda conn: repo.consumed(conn, sessions, scope="execution:p1")) == set()


class Payloads:
    def __init__(self):
        self.data = {}

    def get_session(self, ref):
        return copy.deepcopy(self.data[ref])


def source():
    refs = [{"source_session_key": "one-continuous-root", "trace_id": "trace-" + pid,
             "revision": "r1", "context_revision": "ctx1"} for pid in ("p1", "p2")]
    return {"session_id": "mixed", "source": "langfuse", "runtime": {"type": "observed_http"},
            "runtime_context": {}, "_summary": "Users corrected the two business Prompts",
            "_judge_scores": {"source": "llm", **{key: 0.8 for key in (
                "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage")}},
            "value_judge": {"mode": "merged", "decision": "valuable", "confidence": 0.9, "reason": "Correction"},
            "provenance": {"schema_version": "session-source.v1", "source_session_key": "one-continuous-root",
                           "identity_verified": True, "trace_identity_verified": True,
                           "trace_revisions": refs, "target_trace_refs": refs, "context_trace_refs": []},
            "turns": [{"turn_num": n, "source_trace_id": "trace-" + pid, "prompt_text": "Perform task",
                       "response_text": "Original result", "read_skills": [], "request_context": {
                           "evidence_level": "generation_input", "completeness": "unknown",
                           "execution_binding": {"evidence_level": "upstream_metadata", "execution_profile_id": pid,
                                                 "agent_id": "agent-1", "runtime_type": "observed_http"},
                           "generation_requests": [{"input": [{"role": "system", "content": "Prompt " + pid},
                                                                {"role": "user", "content": "Perform task"}]}],
                       }} for n, pid in enumerate(("p1", "p2"), start=1)]}


def test_native_cycles_consume_only_matching_profile_targets(pg_env, tmp_path, monkeypatch):
    payloads = Payloads()

    async def seed(conn):
        for pid in ("p1", "p2"):
            raw = profile_raw(pid, prefix=pid + "-")
            raw["affected_skill_ids"] = []
            raw["components"] = [{"component_id": pid + "-prompt", "kind": "agent_prompt", "slot": "system",
                                  "order": 0, "content": "Prompt " + pid}]
            await ExecutionRepository(pg_env.tx).register(conn, tenant_id="default", raw=raw,
                                                          skill_dependencies=[], actor="test")
        configs = ConfigRepository(pg_env.tx)
        row = await configs.read_tenant_module(conn, "skills")
        await configs.write_tenant_module(conn, module="skills", expected_revision=row.revision,
                                         config={**row.config, "min_group_sessions": 1, "min_group_users": 1,
                                                 "auto_evaluate": False})
        for sid in ("mixed", "unmatched"):
            session = source()
            if sid == "unmatched":
                for turn in session["turns"]:
                    turn["request_context"]["generation_requests"][0]["input"][0]["content"] = "Unregistered Prompt"
            payloads.data[sid] = session
            await conn.execute(
                f"INSERT INTO {pg_env.schema}.sessions(tenant_id,session_id,user_id,decision,content_ref,content_hash) "
                "VALUES('default',$1,'user-one','valuable',$1,$2)", sid, session_payload_hash(session),
            )

    _run(pg_env, "default", seed)
    observed = []

    async def planner(**kwargs):
        observed.append(kwargs)
        return {"action": "skip", "rationale": "No proposed change"}

    async def synthesize(**kwargs):
        return []

    services = build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(), extras={
        "skill_planner": planner, "replay_case_synthesizer": synthesize, "session_payloads": payloads,
    })
    repo = EvolutionRepository(pg_env.tx)
    with _pod(pg_env, tmp_path, monkeypatch, pod="profile-consumption") as client:
        _login(client, bootstrap=True)
        commands = []
        for pid in ("p1", "p2", "p1"):
            response = client.post("/api/skill-evolution/trigger", headers=H, json={
                "allowed_modules": ["prompt"], "execution_profile_id": pid, "session_ids": ["mixed", "unmatched"],
            })
            assert response.status_code == 202, response.text
            pg_env.tx.pool.run(process_once(services, owner_id="profile-consumption", kinds=["skill.evolve"]))
            command = _run(pg_env, "default", lambda conn: repo.command(conn, response.json()["job_id"]))
            assert command["result"] is not None, command
            commands.append(command)
        assert [item["result"]["received_sessions"] for item in commands] == [1, 1, 0]
        assert len(observed) == 2
        for item, pid in zip(observed, ("p1", "p2")):
            assert item["name"] is None
            assert [s["session_id"] for s in item["sessions"]] == ["mixed"]
            assert [r["trace_id"] for r in item["sessions"][0]["provenance"]["target_trace_refs"]] == ["trace-" + pid]
        rows = _run(pg_env, "default", lambda conn: conn.fetch(
            f"SELECT session_id,consumption_scope FROM {pg_env.schema}.skill_evolution_sessions "
            "ORDER BY consumption_scope"))
        assert [(r["session_id"], r["consumption_scope"]) for r in rows] == [
            ("mixed", "execution:p1"), ("mixed", "execution:p2")]


@pytest.mark.asyncio
async def test_input_filter_scans_past_unrelated_first_page_and_reuses_saved_snapshot(monkeypatch):
    from team_skills.evolution import cycle
    from team_skills.execution.contracts import plan_for, validate_profile

    raw = profile_raw("p1")
    raw["affected_skill_ids"] = []
    raw["components"] = [{"component_id": "main", "kind": "agent_prompt", "slot": "system", "order": 0,
                          "content": "Prompt p1"}]
    profile = validate_profile(raw)
    plan = plan_for(profile, profile["components"])
    payloads = Payloads()
    template = source()
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(201):
        sid = f"session-{index:03}"
        value = copy.deepcopy(template)
        if index < 200:
            for turn in value["turns"]:
                turn["request_context"]["generation_requests"][0]["input"][0]["content"] = "Unrelated Prompt"
        payloads.data[sid] = value
        rows.append({"session_id": sid, "content_ref": sid, "content_hash": session_payload_hash(value),
                     "user_id": "user-one", "ingested_at": now})
    pages = AsyncMock(side_effect=[rows[:200], rows[200:]])
    monkeypatch.setattr(cycle, "SessionQueries", lambda tx: SimpleNamespace(evolution_page=pages))

    @asynccontextmanager
    async def transaction(_):
        yield None

    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=transaction), tasks=SimpleNamespace(require_current=AsyncMock()),
        extras={"session_payloads": payloads},
    )
    repo = SimpleNamespace(branch=AsyncMock(return_value=None), consumed=AsyncMock(return_value=set()),
                           save_branch=AsyncMock())
    command = {**frozen("p1"), "config": {"evolution_max_sessions": 1}, "accepted_at": now.isoformat()}
    command["execution_baseline"]["release"] = {"document": {"execution_plan": plan}}
    task = SimpleNamespace(tenant_id="default", task_id="test-inputs")
    selected = await _inputs(task, services, command, repo)
    assert [row["session_id"] for row in selected] == ["session-200"]
    assert pages.await_count == 2
    repo.branch = AsyncMock(return_value={"sessions": selected})
    services.extras["session_payloads"] = None
    assert await _inputs(task, services, command, repo) == selected
    assert pages.await_count == 2  # A resume cannot reselect against new runtime contents.
