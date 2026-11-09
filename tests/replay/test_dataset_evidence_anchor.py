"""Dataset freezing retains source-anchored requests and rejects later rewrites."""
from __future__ import annotations

import copy
import hashlib

import pytest

from team_replay.datasets.schema import DatasetSchemaError, make_document, normalize_case


def anchored_case():
    query = "  历史为空。\n当前请求：周六的活动能参加吗？\n"
    return {"case_id": "recorded-case", "query": query, "skill_ids": [],
            "checks": [{"id": "R01", "kind": "output", "text": "Respond to the recorded request."}],
            "provenance": {"evidence_anchor": {
                "anchor_id": "example-a", "session_id": "recorded-session", "trace_id": "recorded-trace",
                "recorded_query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            }}}


def test_source_query_bytes_survive_repeated_normalization_and_document_freezing():
    case = anchored_case()
    assert normalize_case(normalize_case(case))["query"] == case["query"]
    document = make_document(dataset_id="anchored-dataset", name="Recorded tasks", skills=[], cases=[case])
    assert document["cases"][0]["query"] == case["query"]
    assert document["cases"][0]["provenance"]["evidence_anchor"] == case["provenance"]["evidence_anchor"]


@pytest.mark.parametrize("rewrite", ["New invented history", "  Trimmed query.  ", " "])
def test_rewriting_a_frozen_anchor_query_is_rejected(rewrite):
    case = anchored_case()
    case["query"] = rewrite
    with pytest.raises(DatasetSchemaError, match="query"):
        normalize_case(case)


def test_nonanchored_cases_retain_the_original_normalization_contract():
    case = copy.deepcopy(anchored_case())
    case["provenance"] = {"source": "ordinary-synthesis"}
    assert normalize_case(case)["query"] == case["query"].strip()


@pytest.mark.postgres
def test_anchor_query_survives_persisted_dataset_freezing(pg_env):
    from team_replay.datasets.repository import DatasetRepository

    case = anchored_case()
    document = make_document(dataset_id="recorded-dataset", name="Recorded tasks", skills=[], cases=[case])

    async def persist(conn):
        repo = DatasetRepository(pg_env.tx)
        await repo.save(conn, tenant_id="default", document=document)
        return await repo.freeze(conn, "recorded-dataset", revision=1)

    frozen = pg_env.tx.run_tenant_tx("default", persist)
    assert frozen["document"]["cases"][0]["query"] == case["query"]
    assert frozen["execution_document"]["cases"][0]["query"] == case["query"]
    assert (frozen["document"]["cases"][0]["provenance"]["evidence_anchor"]
            == case["provenance"]["evidence_anchor"])
