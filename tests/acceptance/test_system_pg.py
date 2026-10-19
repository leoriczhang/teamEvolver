"""real PG: composed routes, durable adapters and Session stages."""

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from session_ingestion.handlers import SessionIngestionDeps, register_handlers
from session_ingestion.payloads import SessionPayloads
from session_ingestion.stages import SessionStages
from teamEvolver.app import build_role_services, create_app
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.runtime.services import build_services
from teamEvolver.runtime.worker import process_once
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="requires PostgreSQL")

SOURCE = """
SOURCE = {"enabled": True, "supported_filters": ["from_timestamp", "to_timestamp"]}
def build_adapter(): return Adapter()
class Adapter:
    def health(self): return {"ok": True}
    def list_session_ids(self, filters, *, max_sessions): return ["one"]
    def iter_session_id_batches(self, filters, *, batch_size): yield ["one"]
    def fetch_session(self, session_id): return {"id": session_id}, []
    def convert_session(self, raw, traces):
        return {"session_id": raw["id"], "user_id": "alice",
                "turns": [{"prompt_text": "Task", "response_text": "Done"}]}
    def close(self): pass
"""


def client(env, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root")
    cfg = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn=os.environ["TEAMEVOLVER_TEST_PG_DSN"],
        storage_pg_schema=env.schema,
        storage_pg_ssl="disable",
    )
    return TestClient(create_app(services=build_role_services(role="api", tx=env.tx, config=cfg)))


ROOT = {"Authorization": "Bearer test-root", "X-Tenant-Id": "alpha"}


@pytest.mark.postgres
@pytest.mark.e2e
def test_production_composition_routes_and_root_tenant_selection(pg_env, monkeypatch):
    with client(pg_env, monkeypatch) as c:
        paths = c.app.openapi()["paths"]
        for path in (
            "/ingest_session",
            "/api/datasource",
            "/api/datasource/code",
            "/api/sessions/page",
            "/api/replay-lab/runs",
            "/api/skill-evolution/prompts",
            "/api/model-settings",
            "/api/storage/status",
        ):
            assert path in paths
        response = c.put(
            "/api/datasource/code", headers=ROOT, json={"file": "alpha.py", "code": SOURCE, "expected_revision": None}
        )
        assert response.status_code == 200, response.text
        assert c.get("/api/datasource", headers=ROOT).json()["available"][0]["revision"] == 1
        beta = {**ROOT, "X-Tenant-Id": "beta"}
        assert c.get("/api/datasource", headers=beta).json()["available"] == []
        assert c.get("/api/datasource", headers={**ROOT, "X-Tenant-Id": "missing"}).status_code == 403
        assert (
            c.put(
                "/api/datasource/code",
                headers=ROOT,
                json={"file": "alpha.py", "code": SOURCE + "\n# update", "expected_revision": None},
            ).status_code
            == 409
        )
        assert c.put("/api/datasource", headers=ROOT, json={"file": "alpha.py", "revision": 1}).status_code == 200
        assert c.post("/api/datasource/pull", headers=ROOT, json={"force_reprocess": True}).status_code == 400
        job = c.post("/api/datasource/pull", headers=ROOT, json={"filters": {}})
        assert job.status_code == 202, job.text
        assert c.get(job.json()["status_url"], headers=ROOT).json()["status"] == "queued"
        assert c.get(job.json()["status_url"], headers=beta).status_code == 404


@pytest.mark.postgres
@pytest.mark.e2e
def test_model_secret_is_encrypted_and_prompt_configuration_is_tenant_scoped(pg_env, monkeypatch):
    with client(pg_env, monkeypatch) as c:
        value = c.put(
            "/api/model-settings",
            headers=ROOT,
            json={"model": "test-model", "base_url": "http://model.test", "api_key": "private-model-key"},
        )
        assert value.status_code == 200, value.text
        assert value.json()["api_key_present"] and "private-model-key" not in value.text
        stored = _run(
            pg_env,
            "alpha",
            lambda conn: conn.fetchval(
                f"SELECT config::text FROM {pg_env.schema}.tenant_configs WHERE module='skills'"
            ),
        )
        assert "private-model-key" not in stored and "sref_" in stored
        cipher = _run(
            pg_env, "alpha", lambda conn: conn.fetchval(f"SELECT ciphertext FROM {pg_env.schema}.secret_refs")
        )
        assert "private-model-key" not in cipher
        assert (
            c.put(
                "/api/skill-evolution/prompts/analyze_session",
                headers=ROOT,
                json={"prompt": "Alpha custom instruction"},
            ).status_code
            == 200
        )
        assert (
            c.get("/api/skill-evolution/prompts/analyze_session", headers=ROOT).json()["effective_prompt"]
            == "Alpha custom instruction"
        )
        assert (
            c.get("/api/skill-evolution/prompts/analyze_session", headers={**ROOT, "X-Tenant-Id": "beta"}).json()[
                "effective_prompt"
            ]
            != "Alpha custom instruction"
        )


@pytest.mark.postgres
@pytest.mark.e2e
def test_session_payloads_survive_restart_and_all_stages_preserve_analysis(pg_env, monkeypatch):
    from tests.support.app import _Registry

    registry = _Registry()

    async def analyze(session):
        session["_summary"] = "Mandatory model output"
        session["_judge_scores"] = {"overall_score": 0.8, "source": "llm"}
        session["value_judge"] = {"decision": "valuable"}
        return session["value_judge"]

    async def traces(session):
        assert session["_summary"] == "Mandatory model output"
        return [{"trace_id": "one", "source": "llm"}]

    with client(pg_env, monkeypatch) as c:
        c.app.state.services.extras["tenant_registry"] = registry
        # Authorizer is assembled with a real registry; seed a hashed tenant token.
        token = "tevt_alpha"
        _run(
            pg_env,
            "alpha",
            lambda conn: conn.execute(
                f"UPDATE {pg_env.schema}.tenants SET agent_token_hash=$1 WHERE tenant_id='alpha'",
                hashlib.sha256(token.encode()).hexdigest(),
            ),
        )
        accepted = c.post(
            "/ingest_session",
            headers={"Authorization": "Bearer " + token},
            json={
                "schema_version": "teamevolver.agent-session.v2",
                "protocol_version": "2.0",
                "session_id": "external",
                "runtime": {"type": "test"},
                "runtime_context": {"user_id": "alice"},
                "business_time": "2026-10-12T10:00:00Z",
                "meta": {"trace_id": "trace-external"},
                "used_skills": ["demo"],
                "turns": [
                    {"prompt_text": "Task", "response_text": "Done", "used_skills": ["demo"]},
                    {"prompt_text": "Check", "response_text": "Checked", "used_skills": []},
                ],
            },
        )
        assert accepted.status_code == 202, accepted.text
    payloads = SessionPayloads(pg_env.tx)

    async def split(session):
        return [session]

    worker = build_services(
        role="worker",
        tx=pg_env.tx,
        handlers=register_handlers(),
        extras={
            "session_ingestion": SessionIngestionDeps(
                payloads=payloads,
                stage_runner=SessionStages(payloads, split, analyze, traces),
            )
        },
    )
    for _ in range(4):
        pg_env.tx.pool.run(process_once(worker, owner_id="worker", tenants=("alpha",)))
    rows = _run(pg_env, "alpha", lambda conn: conn.fetch(f"SELECT * FROM {pg_env.schema}.sessions"))
    assert len(rows) == 1
    assert rows[0]["business_time"].isoformat() == "2026-10-12T10:00:00+00:00"
    identity = set_current_tenant(TenantContext("alpha"))
    try:
        session = SessionPayloads(pg_env.tx).get_session(rows[0]["content_ref"])
        assert session["_summary"] == "Mandatory model output" and session["trace_analysis"]
    finally:
        reset_current_tenant(identity)
    assert _run(pg_env, "beta", lambda conn: conn.fetchval(f"SELECT count(*) FROM {pg_env.schema}.sessions")) == 0
    with client(pg_env, monkeypatch) as c:
        page = c.get("/api/sessions/page?limit=20&offset=0", headers=ROOT)
        assert page.status_code == 200, page.text
        row = page.json()["sessions"][0]
        assert row["num_turns"] == 2
        assert row["meta"]["trace_id"] == "trace-external"
        assert row["used_skills"] == ["demo"]
        detail = c.get(f"/api/sessions/{row['session_id']}/content", headers=ROOT)
        assert detail.status_code == 200, detail.text
        assert detail.json()["turns_available"] is True
        assert detail.json()["turns_source"] == "archive"
        assert [turn["prompt_text"] for turn in detail.json()["turns"]] == ["Task", "Check"]


@pytest.mark.postgres
@pytest.mark.e2e
def test_workbench_current_contract_rejects_stale_save_and_clone_overrides(pg_env, tmp_path, monkeypatch):
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as c:
        _login(c, bootstrap=True)
        assert c.post("/api/skills", headers=H, json={"name": "demo", "description": "Testing"}).status_code == 200
        skill = c.get("/api/replay-lab/skills/demo", headers=H).json()
        assert (
            c.post(
                "/api/replay-lab/skills/demo/save", headers=H, json={"candidate_skill_md": skill["skill_md"]}
            ).status_code
            == 400
        )
        assert (
            c.post(
                "/api/replay-lab/skills/demo/save",
                headers=H,
                json={"candidate_skill_md": skill["skill_md"], "expected_sha256": "stale"},
            ).status_code
            == 409
        )
        saved = c.post(
            "/api/replay-lab/datasets",
            headers=H,
            json={
                "schema_version": "team-replay.dataset",
                "dataset_id": "draft",
                "name": "Empty draft",
                "skills": [],
                "cases": [],
                "metadata": {},
                "source": {},
                "description": "",
            },
        )
        assert saved.status_code == 200, saved.text
        assert c.post("/api/replay-lab/datasets/draft/clone", headers=H, json={"skill_id": "demo"}).status_code == 400
        assert c.post("/api/replay-lab/datasets/draft/clone", headers=H, json={}).status_code == 200
