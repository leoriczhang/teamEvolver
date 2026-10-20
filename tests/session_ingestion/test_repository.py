"""test repository."""

from __future__ import annotations

import pytest

from session_ingestion.handlers import (
    KIND_ANALYZE,
    KIND_ARCHIVE,
    KIND_NORMALIZE,
    KIND_PULL,
    KIND_SPLIT,
    register_handlers,
)
from session_ingestion.queries import SessionQueries
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.schemas import (
    PAYLOAD_SCHEMA_VERSION,
    IngestionTaskPayload,
)


@pytest.mark.asyncio
async def test_repeated_push_dedupes_to_first_acceptance(tx, db, payloads):
    repo = SessionIngestionRepository(tx)
    async with tx.tenant_tx("default") as conn:
        first = await repo.accept(
            conn,
            tenant_id="default",
            ingestion_id="i1",
            source="push",
            external_id="s1",
            dedupe_key="push:s1",
            task_id="t1",
        )
        second = await repo.accept(
            conn,
            tenant_id="default",
            ingestion_id="i2",
            source="push",
            external_id="s1",
            dedupe_key="push:s1",
            task_id="t2",
        )
    assert first == "i1" and second == "i1"  # repeated submit is idempotent


@pytest.mark.asyncio
async def test_session_list_is_bounded_and_keyset_paginated(tx, db, payloads):
    repo = SessionIngestionRepository(tx)
    queries = SessionQueries(tx)
    async with tx.tenant_tx("default") as conn:
        for i in range(3):
            await repo.write_session(
                conn,
                tenant_id="default",
                session_id=f"s{i}",
                decision="valuable",
                business_time=f"2026-10-0{i + 1}T00:00:00+00:00",
            )
    async with tx.tenant_tx("default") as conn:
        page = await queries.list_sessions(conn, limit=2)
    assert len(page) == 2
    # Newest business_time first.
    assert page[0]["business_time"] > page[1]["business_time"]
    assert "ingested_at" in page[0] and "business_time" in page[0]


def test_handlers_register_the_five_session_kinds():
    assert set(register_handlers()) == {
        KIND_PULL,
        KIND_NORMALIZE,
        KIND_SPLIT,
        KIND_ANALYZE,
        KIND_ARCHIVE,
    }


def test_task_payload_hash_is_stable_and_round_trips():
    p = IngestionTaskPayload(
        schema_version=PAYLOAD_SCHEMA_VERSION,
        source="push",
        ingestion_id="i1",
        tenant_id="default",
        external_id="s1",
        user_id="u1",
        account_id="a1",
        config_revision=3,
    )
    again = IngestionTaskPayload.from_json(p.to_json())
    assert again.payload_fingerprint() == p.payload_fingerprint()
    assert again.config_revision == 3
