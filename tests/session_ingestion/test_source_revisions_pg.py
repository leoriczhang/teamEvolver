"""Real PG transactions, isolated schemas, scripted analysis; no external models."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from session_ingestion.handlers import SessionIngestionDeps, register_handlers
from session_ingestion.payloads import SessionPayloads
from session_ingestion.schemas import IngestionTaskPayload
from session_ingestion.source_acceptance import accept_source_item
from session_ingestion.source_repository import SourceRevisionRepository
from session_ingestion.stages import SessionStages
from teamEvolver.runtime import build_services, process_once
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.postgres import _run


def observed(count=6):
    return {"session_id": "upstream", "user_id": "user-one", "business_time": "2026-10-20T00:00:00Z",
            "source_identity": {"source_instance_id": "lab", "upstream_namespace": "project",
                                "identity_verified": True},
            "turns": [{"trace_id": f"trace-{n}", "prompt_text": f"question {n}", "response_text": "answer"}
                      for n in range(1, count + 1)]}


@pytest.mark.postgres
def test_concurrent_source_acceptance_converges_and_rls_separates_tenants(pg_env):
    repo = SourceRevisionRepository(pg_env.tx)

    async def accept(tenant):
        async with pg_env.tx.tenant_tx(tenant) as conn:
            session, identifier, duplicate = await repo.prepare(
                conn, tenant_id=tenant, external_id="upstream", session=observed(),
            )
            if not duplicate:
                await repo.remember(conn, tenant_id=tenant, session=session, ingestion_id=identifier)
            return identifier, duplicate

    async def compete():
        return await asyncio.gather(accept("alpha"), accept("alpha"))

    first, second = pg_env.tx.pool.run(compete())
    assert first[0] == second[0]
    assert sorted([first[1], second[1]]) == [False, True]
    other = pg_env.tx.pool.run(accept("beta"))
    assert other[0] != first[0] and not other[1]
    for tenant in ("alpha", "beta"):
        assert _run(pg_env, tenant, lambda c: c.fetchval(
            f"SELECT count(*) FROM {pg_env.schema}.session_source_snapshots",
        )) == 1


def pipeline(env, *, fail_trace=False, split_topics=False):
    calls = {"analyze": 0, "traces": [], "fail_trace": fail_trace, "dependencies": "prompt-model-v1", "context": []}
    payloads = SessionPayloads(env.tx)

    async def split(session):
        if split_topics:
            from session_ingestion.split import split_session

            async def chat(*args, **kwargs):
                return '{"segments":[{"start":1,"end":1,"topic":"one"},{"start":2,"end":2,"topic":"two"}]}'

            async def factory():
                return SimpleNamespace(chat=chat)

            return await split_session(SimpleNamespace(session_split_enabled=True), session, client_factory=factory)
        return [session]

    async def analyze(session):
        calls["analyze"] += 1
        calls["context"].append(copy.deepcopy(session.get("analysis_context") or {}))
        session.update(value_judge={"decision": "valuable"}, _summary="actual scripted analysis",
                       _judge_scores={"overall_score": 0.8})
        return session["value_judge"]

    async def trace(session):
        calls["traces"].append([turn["trace_id"] for turn in session["turns"]])
        if calls["fail_trace"]:
            calls["fail_trace"] = False
            raise RuntimeError("scripted provider interruption")
        return [{"trace_id": turn["trace_id"], "turn_num": n, "label": "checked"}
                for n, turn in enumerate(session["turns"], 1)]

    async def dependencies():
        return {"effective_prompt_and_model": calls["dependencies"]}

    stages = SessionStages(payloads, split, analyze, trace, analysis_identity=dependencies)
    deps = SessionIngestionDeps(payloads=payloads, stage_runner=stages)
    services = build_services(role="worker", tx=env.tx, handlers=register_handlers(),
                              extras={"session_ingestion": deps})
    return services, calls, deps


def accept(env, services, deps, session):
    parent = IngestionTaskPayload(schema_version="session-ingestion.v1", source="pull", ingestion_id="scan",
                                  tenant_id="alpha", external_id="", user_id="", account_id="team",
                                  config_revision=1, adapter_id="adapter", adapter_revision=1)
    token = set_current_tenant(TenantContext("alpha"))
    try:
        return env.tx.pool.run(accept_source_item(services, SimpleNamespace(tenant_id="alpha"), parent,
                                                 copy.deepcopy(session), "upstream", deps))
    finally:
        reset_current_tenant(token)


def drain(env, services):
    for _ in range(24):
        stats = env.tx.pool.run(process_once(services, owner_id="scripted-worker", tenants=("alpha",),
                                               rand=lambda: 0.0))
        if stats is None:
            break


@pytest.mark.postgres
def test_cross_window_duplicate_and_six_to_seven_only_target_new_trace(pg_env):
    services, calls, deps = pipeline(pg_env)
    assert accept(pg_env, services, deps, observed())
    drain(pg_env, services)
    assert not accept(pg_env, services, deps, observed())
    assert calls["analyze"] == 1
    assert accept(pg_env, services, deps, observed(7))
    drain(pg_env, services)
    assert calls["analyze"] == 2
    assert calls["traces"] == [[f"trace-{n}" for n in range(1, 7)], ["trace-7"]]
    rows = _run(pg_env, "alpha", lambda c: c.fetch(
        f"SELECT content_ref,meta FROM {pg_env.schema}.sessions ORDER BY ingested_at",
    ))
    assert len(rows) == 2
    meta = [json.loads(row["meta"]) for row in rows]
    assert meta[0]["source_session_key"] == meta[1]["source_session_key"]
    assert len(meta[1]["target_trace_refs"]) == 1
    assert len(meta[1]["context_trace_refs"]) == 6
    async def archived(conn):
        raw = await conn.fetchval(f"SELECT content FROM {pg_env.schema}.storage_objects WHERE key=$1",
                                  rows[1]["content_ref"])
        return json.loads(bytes(raw))["session"]
    latest = _run(pg_env, "alpha", archived)
    assert len(latest["turns"]) == 7
    assert latest["trace_analysis"][0]["turn_num"] == 7
    assert latest["trace_analysis_coverage"]["target_count"] == 1


@pytest.mark.postgres
@pytest.mark.parametrize("changed_dependency", [False, True])
def test_analysis_checkpoint_reuse_requires_real_dependency_match(pg_env, changed_dependency):
    services, calls, deps = pipeline(pg_env, fail_trace=True)
    assert accept(pg_env, services, deps, observed(1))
    drain(pg_env, services)
    assert calls["analyze"] == 1 and len(calls["traces"]) == 1
    if changed_dependency:
        calls["dependencies"] = "prompt-model-v2"
    _run(pg_env, "alpha", lambda c: c.execute(
        f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE status='retry_wait'",
    ))
    drain(pg_env, services)
    assert calls["analyze"] == (2 if changed_dependency else 1)
    assert len(calls["traces"]) == 2
    assert _run(pg_env, "alpha", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.sessions")) == 1


@pytest.mark.postgres
def test_split_targets_share_one_source_and_retain_complete_original_context(pg_env, monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_SESSION_SPLIT", raising=False)
    services, calls, deps = pipeline(pg_env, split_topics=True)
    assert accept(pg_env, services, deps, observed(2))
    drain(pg_env, services)
    assert calls["analyze"] == 2
    assert calls["traces"] == [["trace-1"], ["trace-2"]]
    assert len(calls["context"]) == 2
    for context in calls["context"]:
        assert [t["trace_id"] for t in context["turns"]] == ["trace-1", "trace-2"]
        assert context["context_only"] is True
    assert calls["context"][0]["full_snapshot_ref"] == calls["context"][1]["full_snapshot_ref"]
    rows = _run(pg_env, "alpha", lambda c: c.fetch(f"SELECT meta FROM {pg_env.schema}.sessions"))
    assert len(rows) == 2
    meta = [json.loads(row["meta"]) for row in rows]
    assert len({m["source_session_key"] for m in meta}) == 1
    assert all(len(m["target_trace_refs"]) == len(m["context_trace_refs"]) == 1 for m in meta)
