"""MA ranges traverse the existing mandatory Worker chain with PostgreSQL."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from session_ingestion.handlers import SessionIngestionDeps
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.stages import SessionStages
from team_ma.commands import enqueue
from team_ma.repository import MARepository
from team_ma.schemas import validate_config
from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.managed_agents import MAClient
from teamEvolver.runtime import process_once
from teamEvolver.security import SecretBox, SecretRepository

pytestmark = pytest.mark.postgres


def test_child_trajectory_mandatory_stages_restart_dedup_and_old_idle_resume(pg_env):
    repo = MARepository(pg_env.tx)
    secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
    timestamp = datetime.now(timezone.utc).isoformat()
    count = [1]
    reviewed = []

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "sessions":
            return httpx.Response(200, json={"data": []})  # only known historical Sessions
        if path.startswith("agents/"):
            return httpx.Response(200, json={"id": "root", "version": 1, "skills": [], "tools": []})
        if path.endswith("/threads"):
            return httpx.Response(200, json={"data": [{"id": "child", "status": "idle"}]})
        if "/threads/child/events" in path:
            return httpx.Response(200, json={"data": [{
                "id": "child-tool", "type": "agent.tool_use", "name": "read",
                "input": {"path": "/skills/skill-note/SKILL.md"}, "processed_at": timestamp,
            }]})
        if path.endswith("/events"):
            events = []
            for index in range(count[0]):
                events.extend([
                    {"id": f"u{index}", "type": "user.message", "processed_at": timestamp,
                     "content": [{"type": "text", "text": "Write a useful note"}]},
                    {"id": f"a{index}", "type": "agent.message", "processed_at": timestamp,
                     "content": [{"type": "text", "text": "A complete note"}]},
                    {"id": f"e{index}", "type": "session.status_idle", "processed_at": timestamp,
                     "stop_reason": {"type": "end_turn"}},
                ])
            return httpx.Response(200, json={"data": events})
        if path.startswith("sessions/"):
            return httpx.Response(200, json={
                "id": path.split("/")[1], "status": "idle", "tags": [{"key": "te-user", "value": "employee42"}],
                "title": "Write a useful note across rounds",
                "agent": {"type": "agent", "id": "root", "version": 1},
                "environment": {"id": "env", "config": {"type": "cloud"}},
            })
        raise AssertionError(path)

    def worker():
        services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
        services.extras["secret_repo"] = secrets
        services.extras["ma_client_factory"] = lambda url, key: MAClient(
            url, key, transport=httpx.MockTransport(respond),
        )

        async def split(session):
            assert session["runtime"]["agent_graph"]["agents"]["root"]["version"] == 1
            return [session]

        async def analyze(session):
            reviewed.append(session)
            session.update(value_judge={"decision": "valuable"}, _judge_scores={"overall_score": 8})
            return {"decision": "valuable"}

        async def trace(session):
            return {"analyzed": True}

        payloads = services.extras["session_payloads"]
        services.extras["session_ingestion"] = SessionIngestionDeps(
            payloads=payloads, stage_runner=SessionStages(payloads, split, analyze, trace),
        )
        return services

    async def run():
        services = worker()
        async with pg_env.tx.tenant_tx("alpha") as conn:
            ma = await secrets.store(conn, tenant_id="alpha", plaintext="ark")
            token = await secrets.store(conn, tenant_id="alpha", plaintext="tevt_test")
            config = validate_config({
                "display_name": "Generic", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "fallback", "ma_secret_ref": ma.ref, "tenant_token_ref": token.ref,
            })
            await repo.save(conn, tenant_id="alpha", connection_id="connection", config=config)
            await repo.status(conn, "connection", "ready")
            await repo.bind(conn, tenant_id="alpha", connection_id="connection", kind="skill", name="note-craft",
                            record={"id": "skill-note", "agents": ["root"]})
            # More than a page of known idle Sessions, all absent from the fresh remote list.
            for index in range(51):
                await repo.save_cursor(conn, tenant_id="alpha", connection_id="connection",
                                       session_id=f"historical-{index:03}", record={"status": "idle"})
            await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.collect",
                          operation_id="collect")
        for _ in range(3):
            await process_once(services, owner_id="collector", tenants=("alpha",), kinds=("ma.collect",))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            cursors = await repo.cursors(conn, "connection")
            assert len(cursors) == 51 and all(c["record"].get("range_id") for c in cursors)
        services = worker()  # fresh container, persisted payloads and checkpoints survive
        while await process_once(services, owner_id="restarted", tenants=("alpha",),
                                kinds=("session.normalize", "session.split", "session.analyze", "session.archive")):
            pass
        assert len(reviewed) == 51
        assert reviewed[0]["runtime_context"]["user_attribution"] == "tag"
        assert reviewed[0]["title"] == "Write a useful note across rounds"
        assert reviewed[0]["turns"][0]["used_skills"] == ["note-craft"]
        ingestion = SessionIngestionRepository(pg_env.tx)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            cursor = await repo.cursor(conn, "connection", "historical-050")
            ingestion_id = cursor["record"]["job_id"].removesuffix(":session.normalize")
            stages = await ingestion.succeeded_stages(conn, ingestion_id=ingestion_id)
            assert stages == {"normalize", "split", "analyze", "trace_analyze", "archive"}
            await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.collect",
                          body={"session_ids": ["historical-050"]}, operation_id="duplicate")
        await process_once(services, owner_id="collector", tenants=("alpha",), kinds=("ma.collect",))
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.operation(conn, "duplicate"))["result"]["counts"] == {"unchanged": 1}
            legacy = dict((await repo.cursor(conn, "connection", "historical-050"))["record"])
            legacy.pop("formal_session_id")
            await repo.save_cursor(
                conn, tenant_id="alpha", connection_id="connection",
                session_id="historical-050", record=legacy,
            )
            await enqueue(
                conn, services, tenant_id="alpha", connection_id="connection", kind="ma.collect",
                body={"session_ids": ["historical-050"]}, operation_id="legacy-range-migration",
            )
        await process_once(services, owner_id="collector", tenants=("alpha",), kinds=("ma.collect",))
        while await process_once(services, owner_id="legacy-stages", tenants=("alpha",),
                                 kinds=("session.normalize", "session.split", "session.analyze",
                                        "session.archive")):
            pass
        async with pg_env.tx.tenant_tx("alpha") as conn:
            assert (await repo.operation(conn, "legacy-range-migration"))["result"]["counts"] == {"accepted": 1}
        count[0] = 2  # the same old idle Session resumes with another interaction
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.collect",
                          body={"session_ids": ["historical-050"]}, operation_id="resumed")
        await process_once(services, owner_id="collector", tenants=("alpha",), kinds=("ma.collect",))
        while await process_once(services, owner_id="resumed-stages", tenants=("alpha",),
                                 kinds=("session.normalize", "session.split", "session.analyze",
                                        "session.archive")):
            pass
        async with pg_env.tx.tenant_tx("alpha") as conn:
            resumed = await repo.cursor(conn, "connection", "historical-050")
            assert resumed["record"]["end_event_id"] == "e1"
            assert resumed["record"]["range_id"] != cursor["record"]["range_id"]
            formal = await conn.fetch(
                f"SELECT session_id,meta FROM {pg_env.schema}.sessions "
                "WHERE meta->>'ma_parent_session_id'='historical-050'",
            )
            assert len(formal) == 1
            meta = json.loads(formal[0]["meta"]) if isinstance(formal[0]["meta"], str) else formal[0]["meta"]
            assert meta["num_turns"] == 2
    asyncio.run(run())
