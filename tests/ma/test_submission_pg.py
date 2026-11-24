"""Channel metadata repairs reuse analysis and immutable source evidence."""
import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from session_ingestion.handlers import SessionIngestionDeps, handle_archive
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
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant

pytestmark = pytest.mark.postgres


def test_replacing_formal_session_updates_owner_and_keeps_other_tenant(pg_env):
    async def run():
        repo = SessionIngestionRepository(pg_env.tx)
        for tenant in ("alpha", "beta"):
            async with pg_env.tx.tenant_tx(tenant) as conn:
                await repo.write_session(
                    conn, tenant_id=tenant, session_id="same", user_id="old",
                    account_id=tenant, external_id="range1",
                )
        async with pg_env.tx.tenant_tx("alpha") as conn:
            await repo.write_session(
                conn, tenant_id="alpha", session_id="same", user_id="actual",
                account_id="account2", external_id="range2",
            )
            row = await conn.fetchrow(
                f"SELECT user_id,account_id,external_id FROM {pg_env.schema}.sessions WHERE session_id='same'",
            )
            assert dict(row) == {"user_id": "actual", "account_id": "account2", "external_id": "range2"}
        async with pg_env.tx.tenant_tx("beta") as conn:
            assert await conn.fetchval(
                f"SELECT user_id FROM {pg_env.schema}.sessions WHERE session_id='same'",
            ) == "old"
    asyncio.run(run())


@pytest.mark.parametrize("with_channel", [True, False])
def test_channel_collection_repairs_legacy_metadata_without_reanalysis(pg_env, with_channel):
    timestamp = datetime.now(timezone.utc).isoformat()
    raw = ('<ChannelContext provider="feishu" app_id="cli_one" sender_open_id="ou_alice" sender_name="Alice"/>'
           '\n<ContextReadingPolicy>policy</ContextReadingPolicy>\n@Seedance Ma Bot 给我个视频')
    if not with_channel:
        raw = "给我个视频"
    tags = []
    reviews = []

    def respond(request):
        path = request.url.path.removeprefix("/api/v3/")
        if path == "sessions":
            return httpx.Response(200, json={"data": []})
        if path.startswith("agents/"):
            return httpx.Response(200, json={"id": "root", "version": 1, "skills": [], "tools": []})
        if path.endswith("/threads"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/events"):
            return httpx.Response(200, json={"data": [
                {"id": "u", "type": "user.message", "processed_at": timestamp,
                 "content": [{"type": "text", "text": raw}]},
                {"id": "a", "type": "agent.message", "processed_at": timestamp,
                 "content": [{"type": "text", "text": "done"}]},
                {"id": "e", "type": "session.status_idle", "processed_at": timestamp,
                 "stop_reason": {"type": "end_turn"}},
            ]})
        if path == "sessions/source":
            return httpx.Response(200, json={
                "id": "source", "status": "idle", "tags": tags,
                "agent": {"id": "root", "version": 1},
                "environment": {"id": "env", "config": {"type": "cloud"}},
            })
        raise AssertionError(path)

    async def run():
        services = build_role_services(role="worker", tx=pg_env.tx, config=TeamEvolverConfig())
        secrets = SecretRepository(pg_env.tx, SecretBox("test-key"))
        services.extras["secret_repo"] = secrets
        services.extras["ma_client_factory"] = lambda url, key: MAClient(
            url, key, transport=httpx.MockTransport(respond),
        )

        async def split(session):
            return [session]

        async def analyze(session):
            reviews.append(session)
            session.update(value_judge={"decision": "valuable"}, _judge_scores={"overall_score": 8})
            return {"decision": "valuable"}

        async def trace(session):
            return {"analyzed": True}

        payloads = services.extras["session_payloads"]
        services.extras["session_ingestion"] = SessionIngestionDeps(
            payloads=payloads, stage_runner=SessionStages(payloads, split, analyze, trace),
        )
        ma, ingestion = MARepository(pg_env.tx), SessionIngestionRepository(pg_env.tx)
        async with pg_env.tx.tenant_tx("alpha") as conn:
            secret = await secrets.store(conn, tenant_id="alpha", plaintext="ark")
            token = await secrets.store(conn, tenant_id="alpha", plaintext="tevt_test")
            config = validate_config({
                "display_name": "Generic", "entry_agent_id": "root", "environment_id": "env",
                "default_user_id": "operator", "ma_secret_ref": secret.ref, "tenant_token_ref": token.ref,
            })
            await ma.save(conn, tenant_id="alpha", connection_id="connection", config=config)
            await ma.status(conn, "connection", "ready")

        async def collect(ident):
            async with pg_env.tx.tenant_tx("alpha") as conn:
                await enqueue(conn, services, tenant_id="alpha", connection_id="connection", kind="ma.collect",
                              body={"session_ids": ["source"]}, operation_id=ident)
            await process_once(services, owner_id="collector", tenants=("alpha",), kinds=("ma.collect",))

        await collect("initial")
        while await process_once(services, owner_id="stages", tenants=("alpha",),
                                kinds=("session.normalize", "session.split", "session.analyze", "session.archive")):
            pass
        async with pg_env.tx.tenant_tx("alpha") as conn:
            cursor = await ma.cursor(conn, "connection", "source")
            formal_id = cursor["record"]["formal_session_id"]
            row = dict(await conn.fetchrow(f"SELECT * FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id))
            assert row["user_id"] == ("feishu.cli_one.ou_alice" if with_channel else "operator")
            assert json.loads(row["meta"])["title"] == "给我个视频"
        ctx = set_current_tenant(TenantContext(tenant_id="alpha"))
        try:
            original = await asyncio.to_thread(payloads.get_session, row["content_ref"])
            stale = copy.deepcopy(original)
            stale.update(title=raw[:120], user_id="operator", user_alias="operator")
            stale["meta"].update(title=raw[:120], user_id="operator", user_display_name="")
            stale["runtime_context"]["user_id"] = "operator"
            ref, sha = await asyncio.to_thread(payloads.put_session, row["ingestion_id"], stale, stage="legacy")
            async with pg_env.tx.tenant_tx("alpha") as conn:
                await ingestion.write_session(
                    conn, tenant_id="alpha", session_id=formal_id, ingestion_id=row["ingestion_id"],
                    user_id="operator", account_id="alpha", content_ref=ref, content_hash=sha,
                    meta=stale["meta"], business_time=timestamp,
                )
                before = dict(await conn.fetchrow(
                    f"SELECT * FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id,
                ))
                stages = await ingestion.stage_results(conn, ingestion_id=row["ingestion_id"])
                old_cursor = dict(cursor["record"])
                old_cursor.pop("submission_revision")
                await ma.save_cursor(conn, tenant_id="alpha", connection_id="connection",
                                     session_id="source", record=old_cursor)
                if not with_channel:
                    current = await ma.connection(conn, "connection")
                    await ma.save(conn, tenant_id="alpha", connection_id="connection",
                                  config={**current["config"], "default_user_id": ""},
                                  expected_revision=current["revision"])
                    await ma.status(conn, "connection", "ready")
            await collect("repair")
            async with pg_env.tx.tenant_tx("alpha") as conn:
                after = dict(await conn.fetchrow(
                    f"SELECT * FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id,
                ))
                if with_channel:
                    assert after["user_id"] == "feishu.cli_one.ou_alice"
                    assert json.loads(after["meta"])["user_display_name"] == "Alice"
                else:
                    assert after["user_id"].startswith("unknown.")
                    assert json.loads(after["meta"])["user_display_name"] == "未知提交人"
                    assert json.loads(after["meta"])["user_attribution"] == "identity_unavailable"
                assert json.loads(after["meta"])["title"] == "给我个视频"
                assert after["business_time"] == before["business_time"]
                assert after["ingested_at"] == before["ingested_at"]
                assert after["content_ref"] != before["content_ref"]
                assert await ingestion.stage_results(conn, ingestion_id=row["ingestion_id"]) == stages
            assert await asyncio.to_thread(payloads.get_session, ref) == stale
            assert len(reviews) == 1
            await collect("repeat")
            async with pg_env.tx.tenant_tx("alpha") as conn:
                assert (await ma.operation(conn, "repeat"))["result"]["counts"] == {"unchanged": 1}
                assert await conn.fetchval(
                    f"SELECT content_ref FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id,
                ) == after["content_ref"]
            tags.append({"key": "te-user", "value": "employee42"})
            await collect("tag-change")
            async with pg_env.tx.tenant_tx("alpha") as conn:
                updated = await conn.fetchrow(
                    f"SELECT user_id,meta FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id,
                )
                assert updated["user_id"] == "employee42"
                assert json.loads(updated["meta"])["user_display_name"] == ""
                archived_task = await conn.fetchrow(
                    f"SELECT * FROM {pg_env.schema}.worker_tasks WHERE task_id=$1",
                    row["ingestion_id"] + ":session.archive",
                )
            assert (await handle_archive(SimpleNamespace(**archived_task), services)).outcome == "succeed"
            async with pg_env.tx.tenant_tx("alpha") as conn:
                assert await conn.fetchval(
                    f"SELECT user_id FROM {pg_env.schema}.sessions WHERE session_id=$1", formal_id,
                ) == "employee42"
            assert len(reviews) == 1
        finally:
            reset_current_tenant(ctx)
    asyncio.run(run())
