#!/usr/bin/env python3
"""10k synthetic documents over real HTTP; fixture extraction is explicitly not LLM quality/cost."""

import argparse
import concurrent.futures
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=10000)
    args = parser.parse_args()
    root = Path(os.environ.get("ONTOLOGY_LAB_STATE", "/tmp/te-ontology-lab"))
    keys = json.loads((root / "credentials.json").read_text())
    client = httpx.Client(
        base_url="http://127.0.0.1:52111/api/v1/enterprise",
        timeout=60,
        trust_env=False,
        headers={"Authorization": "Bearer " + keys["other"]},
    )
    run = "load-" + uuid.uuid4().hex[:10]
    latencies, batches = [], []

    def call(method, path, data=None):
        response = client.request(method, path, json=data)
        response.raise_for_status()
        return response.json()

    schema = {
        "revision": run,
        "entity_types": ["DocumentSubject"],
        "predicates": {"state": {"subject_type": "DocumentSubject"}},
        "rules": [{"rule_id": "ready", "predicate": "state", "equals": "ready"}],
        "issue_pack_revision": run,
        "evidence_slots": ["state"],
    }
    call("POST", "/schemas", schema)
    started = time.monotonic()
    for offset in range(0, args.documents, 100):
        batch_start = time.monotonic()
        refs = []
        for i in range(offset, min(offset + 100, args.documents)):
            entity_id = run + ":" + str(i)
            record = {
                "entity": {
                    "entity_id": entity_id,
                    "authority_namespace": run,
                    "entity_type": "DocumentSubject",
                    "external_id": str(i),
                    "label": str(i),
                },
                "assertion": {
                    "assertion_id": entity_id,
                    "subject": entity_id,
                    "predicate": "state",
                    "value": "ready",
                    "valid_from": "2026-01-01T00:00:00Z",
                },
                "quote": "ready",
            }
            refs.append(
                call(
                    "POST",
                    "/sources",
                    {"source_id": entity_id, "revision": "r1", "text": json.dumps([record]), "readers": ["reviewer"]},
                )
            )
        head = call("GET", "/ontology/capabilities")["generation"]
        manifest = call(
            "POST",
            "/manifests",
            {
                "schema_revision": run,
                "sources": refs,
                "expected_generation": head,
                "mode": "replace" if offset == 0 else "delta",
                "extractor": "fixture",
            },
        )
        key = f"{run}-{offset}"
        task = call(
            "POST",
            "/compile-submissions",
            {"submission_key": key, "manifest_ref": manifest["id"], "manifest_digest": manifest["digest"]},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = call("GET", "/tasks/" + task["task_id"])
            if result["state"] == "review_ready":
                break
            if result["state"] in {"failed", "cancelled"}:
                raise RuntimeError(result["state"])
            time.sleep(0.1)
        if result["state"] != "review_ready":
            raise RuntimeError("load task deadline")
        prepared = call(
            "POST", "/assets/prepare", {"task_id": task["task_id"], "candidate_digest": result["candidate_digest"]}
        )
        grant = call(
            "POST",
            "/assets/approvals",
            {"prepared_id": prepared["prepared_id"], "note": "Synthetic load fixture; automated test approval only"},
        )
        call(
            "POST",
            "/assets/commits",
            {"prepared_id": prepared["prepared_id"], "commit_key": key, "grant": grant["grant"]},
        )
        batches.append(time.monotonic() - batch_start)
        if offset % 1000 == 0 or offset + 100 >= args.documents:

            def read(_):
                before = time.monotonic()
                packet = call(
                    "POST", "/context/compose", {"entity_ids": [run + ":" + str(offset)], "token_budget": 4000}
                )
                assert packet["facts"]
                return (time.monotonic() - before) * 1000

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                latencies.extend(pool.map(read, range(10)))
            print(
                json.dumps(
                    {
                        "completed_documents": min(offset + 100, args.documents),
                        "elapsed_s": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
    report = {
        "scope": "real OV HTTP + PostgreSQL + separate Runtime; synthetic fixture extraction",
        "run": run,
        "documents": args.documents,
        "assertions": args.documents,
        "batches": len(batches),
        "duration_seconds": round(time.monotonic() - started, 3),
        "context_concurrency": 10,
        "context_p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 3),
        "batch_median_seconds": round(statistics.median(batches), 3),
        "model_calls": 0,
        "model_tokens": 0,
        "human_review_seconds": None,
        "limitations": [
            "automated fixture approval is not human review",
            "no real LLM quality or cost measured",
            "no hybrid vector query measured",
        ],
    }
    (root / "load-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
