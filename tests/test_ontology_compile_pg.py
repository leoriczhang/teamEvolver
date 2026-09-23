"""Opt-in isolated PostgreSQL tests; creates and drops only its own random schema."""

import asyncio
import os
import secrets

import asyncpg
import pytest
from fastapi import HTTPException

from team_ontology.compile_contract import SOURCE_CONTRACT
from team_ontology.control import ControlStore

DSN = os.environ.get("ONTOLOGY_COMPILE_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires isolated ONTOLOGY_COMPILE_TEST_DSN")


@pytest.mark.asyncio
async def test_durable_checkpoints_restart_idempotency_and_cancel_fencing():
    schema = "test_compile_" + secrets.token_hex(6)
    store = ControlStore(DSN, schema=schema)
    other = ControlStore(DSN, schema=schema)
    p = {"tenant": "a", "subject": "reviewer"}
    request = {"contract": SOURCE_CONTRACT, "roots": ["viking://resources/wiki"]}
    try:
        await store.start()
        rows = await asyncio.gather(*(store.submit(p, "same", request) for _ in range(5)))
        assert len({r["id"] for r in rows}) == 1
        identifier = rows[0]["id"]
        await store.pool.execute(f"UPDATE {store.jobs} SET updated=now()-interval '2 seconds'")
        claimed = await asyncio.gather(store.claim(), store.claim())
        job = next(r for r in claimed if r)
        assert sum(r is not None for r in claimed) == 1
        await store.checkpoint(job, {"stage": "freeze", "index": 3, "refs": []})
        await store.close()
        await other.start()
        persisted = await other.get(p, identifier)
        assert persisted["result"]["index"] == 3
        await other.pool.execute(f"UPDATE {other.jobs} SET updated=now()-interval '2 seconds'")
        resumed = await other.claim()
        assert resumed["attempt"] == 2
        await other.change(p, identifier, ["running"], "cancelled")
        with pytest.raises(HTTPException, match="JOB_STATE_CHANGED"):
            await other.checkpoint(resumed, {"stage": "review_ready"}, "review_ready")
        with pytest.raises(HTTPException, match="JOB_NOT_FOUND"):
            await other.get({"tenant": "b", "subject": "reviewer"}, identifier)
        other.limit = 1
        uncertain = await other.submit(p, "uncertain", request)
        await other.change(p, uncertain["id"], ["queued"], "compile_unknown")
        with pytest.raises(HTTPException, match="ONTOLOGY_QUEUE_FULL"):
            await other.submit(p, "third", request)
        with pytest.raises(HTTPException, match="ONTOLOGY_QUEUE_FULL"):
            await other.resume(p, identifier, ["cancelled"], {})
    finally:
        await store.close()
        await other.close()
        conn = await asyncpg.connect(DSN)
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
