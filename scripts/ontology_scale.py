#!/usr/bin/env python3
"""Synthetic projection sensitivity at 100k/1m/5m rows. Not an ingestion benchmark."""

import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

OV = Path(os.environ.get("ONTOLOGY_OV_REPO", Path(__file__).resolve().parents[2] / "OpenViking"))
sys.path.insert(0, str(OV))
from openviking.ontology.contracts import ContextRequest, Schema, Source, canonical  # noqa: E402
from openviking.ontology.service import OntologyService  # noqa: E402
from openviking.ontology.store import Principal, Store  # noqa: E402


async def main():
    store = Store("postgresql://localhost:55439/ontology_lab")
    await store.start()
    tenant = "scale-" + secrets.token_hex(6)
    principal = Principal(tenant, "bench")
    service = OntologyService(store, "isolated-scale-signing-secret-" + "x" * 32)
    async with store.pool.acquire() as conn:
        await conn.execute("INSERT INTO ov_semantic.tenants(tenant,enabled) VALUES($1,true)", tenant)
        await conn.execute(
            "INSERT INTO ov_semantic.principals(tenant,subject,permissions) "
            "VALUES($1,'bench',ARRAY['read','build','approve'])",
            tenant,
        )
    await service.schema(
        principal,
        Schema(
            revision="scale",
            entity_types=["Sample"],
            predicates={"state": {"subject_type": "Sample"}},
            issue_pack_revision="scale",
            evidence_slots=["state"],
        ),
    )
    ref = await service.source(principal, Source(source_id="synthetic", revision="1", text="ready", readers=["bench"]))
    fact = {
        "assertion_id": "a0",
        "subject": "s0",
        "predicate": "state",
        "value": "ready",
        "polarity": "positive",
        "qualifiers": {},
        "epistemic_kind": "document_fact",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": None,
        "recorded_from": "2026-09-19T00:00:00Z",
        "proofs": [[{**ref, "evidence_id": "e1", "start": 0, "end": 5, "quote": "ready"}]],
    }
    results, previous = [], 0
    try:
        async with store.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ov_semantic.generations(tenant,generation,schema_revision,source_watermark) "
                "VALUES($1,1,'scale','[]')",
                tenant,
            )
            await conn.execute("UPDATE ov_semantic.tenants SET generation=1 WHERE tenant=$1", tenant)
        for count in (100000, 1000000, 5000000):
            began = time.monotonic()
            async with store.pool.acquire() as conn:
                await conn.execute("SET statement_timeout='600s'")
                await conn.execute(
                    "INSERT INTO ov_semantic.facts(tenant,generation,id,subject,predicate,body) "
                    "SELECT $1,1,'a'||n,'s'||n,'state',"
                    "jsonb_set(jsonb_set($2::jsonb,'{subject}',to_jsonb('s'||n)),'{assertion_id}',to_jsonb('a'||n)) "
                    "FROM generate_series($3::bigint,$4::bigint) n",
                    tenant,
                    canonical(fact),
                    previous,
                    count - 1,
                    timeout=600,
                )
                await conn.execute("ANALYZE ov_semantic.facts", timeout=60)
            load_seconds = time.monotonic() - began

            async def query(index):
                before = time.monotonic()
                packet = await service.compose(principal, ContextRequest(entity_ids=[f"s{(index * 99991) % count}"]))
                assert len(packet["facts"]) == 1
                return (time.monotonic() - before) * 1000

            latencies = []
            for repeat in range(10):
                latencies.extend(await asyncio.gather(*(query(repeat * 10 + i) for i in range(10))))
            result = {
                "assertions": count,
                "concurrency": 10,
                "requests": 100,
                "p95_ms": round(sorted(latencies)[94], 3),
                "insert_seconds": round(load_seconds, 3),
            }
            results.append(result)
            print(json.dumps(result), flush=True)
            previous = count
    finally:
        async with store.pool.acquire() as conn:
            for table in ("facts", "generations", "objects", "source_status", "principals"):
                await conn.execute(f"DELETE FROM ov_semantic.{table} WHERE tenant=$1", tenant, timeout=600)
            await conn.execute("DELETE FROM ov_semantic.tenants WHERE tenant=$1", tenant)
        await store.close()
    report = {
        "scope": "synthetic PG projection sensitivity; direct service calls, not publication or HTTP throughput",
        "results": results,
        "limitations": [
            "one shared authorized evidence source",
            "uniform one-fact subjects",
            "no LLM calls",
            "no production concurrency mix",
        ],
    }
    root = Path(os.environ.get("ONTOLOGY_LAB_STATE", "/tmp/te-ontology-lab"))
    (root / "scale-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
