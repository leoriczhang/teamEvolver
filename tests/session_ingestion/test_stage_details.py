"""Stage output projections and tenant-scoped lazy console reads."""
import os

import pytest
from fastapi.testclient import TestClient

from session_ingestion.payloads import SessionPayloads
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.stage_projection import stage_result
from teamEvolver.app import build_role_services, create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.postgres import _run


def test_stage_projection_preserves_findings_and_does_not_copy_runtime_or_raw_content():
    session = {
        "runtime": {"env": {"API_KEY": "private"}}, "system_prompt": "private",
        "turns": [{"prompt_text": "raw request", "tool_calls": [{"arguments": "private"}]}],
        "_summary": "Useful findings", "_judge_scores": {"overall_score": .8},
        "trace_analysis": [{"trace_id": f"trace-{i}", "is_badcase": True, "problem_description": "Tool failed",
                            "raw": "private"} for i in range(300)],
    }
    assert stage_result("normalize", session) == {"overview": {"turn_count": 1, "tool_call_count": 1}}
    assert stage_result("analyze", session)["summary"] == "Useful findings"
    traces = stage_result("trace_analyze", session)["traces"]
    assert len(traces) == 300 and all("raw" not in trace for trace in traces)
    assert traces[-1]["trace_id"] == "trace-299"
    assert stage_result("split", {"segments": [session]}) == {
        "segment_count": 1, "segments": [{"turn_count": 1}],
    }


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires isolated PostgreSQL")
def test_stage_details_read_exact_output_and_enforce_tenant_and_admin(pg_env, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root")
    repo, payloads = SessionIngestionRepository(pg_env.tx), SessionPayloads(pg_env.tx)
    identity = set_current_tenant(TenantContext("alpha"))
    try:
        ref, _ = payloads.put_session("ingest-1", {
            "_summary": "Actual stage output", "_judge_scores": {"overall_score": .7},
            "user_id": "previous-owner",
            "runtime": {"env": {"API_KEY": "private"}}, "turns": [{"prompt_text": "raw request"}],
        }, stage="session-analysis")
    finally:
        reset_current_tenant(identity)

    async def setup(conn):
        await repo.accept(conn, tenant_id="alpha", ingestion_id="ingest-1", source="push")
        await repo.write_session(conn, tenant_id="alpha", session_id="session-1", ingestion_id="ingest-1",
                                 decision="valuable", user_id="corrected-owner")
        for stage in ("archive", "analyze", "normalize", "trace_analyze", "split"):
            await repo.record_stage(conn, tenant_id="alpha", ingestion_id="ingest-1", stage=stage,
                                    status="succeeded", result_ref=ref if stage == "analyze" else "",
                                    detail={"decision": "valuable"} if stage == "analyze" else {})
    _run(pg_env, "alpha", setup)
    cfg = TeamEvolverConfig(storage_pg_enabled=True, storage_pg_dsn=os.environ["TEAMEVOLVER_TEST_PG_DSN"],
                           storage_pg_schema=pg_env.schema, storage_pg_ssl="disable")
    app = create_app(services=build_role_services(role="api", tx=pg_env.tx, config=cfg))
    headers = {"Authorization": "Bearer test-root", "X-Tenant-Id": "alpha"}
    with TestClient(app) as client:
        path = "/api/sessions/session-1/stages"
        stages = client.get(path, headers=headers).json()["stages"]
        assert [s["stage"] for s in stages] == ["normalize", "split", "analyze", "trace_analyze", "archive"]
        assert all("result" not in s and "result_ref" not in s for s in stages)
        response = client.get(path + "/analyze", headers=headers)
        assert response.status_code == 200
        value = response.json()
        assert value["result_available"] and value["result"]["summary"] == "Actual stage output"
        assert value["detail"] == {"decision": "valuable"}
        assert all(text not in response.text for text in ("private", "raw request", "result_ref"))
        assert client.get(path + "/analyze", headers={**headers, "X-Tenant-Id": "beta"}).status_code == 404
        assert client.get(path + "/analyze").status_code in {401, 403}
        assert client.get(path + "/missing", headers=headers).status_code == 404
        unavailable = client.get(path + "/normalize", headers=headers).json()
        assert not unavailable["result_available"] and unavailable["result"] is None
        _run(pg_env, "alpha", lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.session_stage_results SET result_ref=$1 WHERE stage='normalize'", ref))
        normalized = client.get(path + "/normalize", headers=headers).json()
        assert normalized["result"]["overview"]["user_id"] == "corrected-owner"
        _run(pg_env, "alpha", lambda conn: conn.execute(
            f"UPDATE {pg_env.schema}.session_stage_results SET result_ref='missing.json' WHERE stage='normalize'"))
        unavailable = client.get(path + "/normalize", headers=headers).json()
        assert not unavailable["result_available"] and unavailable["result_unavailable_reason"]
