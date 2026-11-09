"""Real PG API checks: preview writes nothing, conflicts and tenant boundaries remain intact."""
from __future__ import annotations

import os

import pytest

from session_ingestion.payloads import SessionPayloads
from session_ingestion.repository import SessionIngestionRepository
from session_ingestion.schemas import session_payload_hash
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant
from tests.support.app import H, _login, _pod
from tests.support.execution import profile_raw
from tests.support.postgres import _run

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL not configured"),
]
URL = "/api/skill-evolution/execution-profiles"


def _counts(env):
    async def read(conn):
        return {table: await conn.fetchval(f"SELECT count(*) FROM {env.schema}.{table}") for table in (
            "execution_profiles", "execution_assets", "execution_asset_versions", "execution_releases", "worker_tasks",
        )}
    return _run(env, "default", read)


def test_preview_registration_and_copyable_live_agent_contract_are_read_only(pg_env, tmp_path, monkeypatch):
    raw = profile_raw("registration-preview")
    raw["affected_skill_ids"] = []
    initial = _counts(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="registration-preview") as client:
        _login(client, bootstrap=True)
        response = client.post(URL + "/preview", headers=H, json=raw)
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["component_count"] == 2
        assert _counts(pg_env) == initial
        assert client.get(URL + "/registration-preview/integration", headers=H).status_code == 404
        created = client.post(URL, headers=H, json=raw)
        assert created.status_code == 201, created.text
        after = _counts(pg_env)
        contract = client.get(URL + "/registration-preview/integration", headers=H)
        assert contract.status_code == 200, contract.text
        assert contract.json()["current_release_id"] == created.json()["current_release_id"]
        assert contract.json()["shared_template"] is False
        assert client.post(URL + "/preview", headers=H, json=raw).status_code == 409
        assert _counts(pg_env) == after
        isolated = client.get(URL + "/registration-preview/integration", headers={"X-Tenant-Id": "alpha"})
        assert isolated.status_code == 404
        collision = {**raw, "profile_id": "different-profile"}
        assert client.post(URL + "/preview", headers=H, json=collision).status_code == 409
        assert _counts(pg_env) == after


def test_preview_and_registration_both_require_existing_affected_skills(pg_env, tmp_path, monkeypatch):
    raw = profile_raw("missing-skill")
    before = _counts(pg_env)
    with _pod(pg_env, tmp_path, monkeypatch, pod="registration-missing-skill") as client:
        _login(client, bootstrap=True)
        preview = client.post(URL + "/preview", headers=H, json=raw)
        create = client.post(URL, headers=H, json=raw)
        assert preview.status_code == create.status_code == 400
    assert _counts(pg_env) == before


def test_session_match_diagnostic_reads_archived_request_without_starting_an_evolution(pg_env, tmp_path, monkeypatch):
    raw = profile_raw("match-diagnostic")
    raw["affected_skill_ids"] = []
    session = {
        "session_id": "observed-session", "runtime_context": {"execution_profile_id": "match-diagnostic"},
        "provenance": {"identity_verified": True}, "turns": [{"request_context": {
            "evidence_level": "generation_input", "messages": [{"role": "system",
                "content": raw["components"][0]["content"]}, {"role": "developer",
                "content": raw["components"][1]["content"]}],
        }}],
    }
    payloads = SessionPayloads(pg_env.tx)
    token = set_current_tenant(TenantContext("default"))
    try:
        ref, sha = payloads.put_session("diagnostic-ingestion", session, stage="archive")
    finally:
        reset_current_tenant(token)
    assert sha == session_payload_hash(session)

    async def seed(conn):
        repo = SessionIngestionRepository(pg_env.tx)
        await repo.accept(conn, tenant_id="default", ingestion_id="diagnostic-ingestion", source="pull")
        await repo.write_session(conn, tenant_id="default", ingestion_id="diagnostic-ingestion",
                                 session_id=session["session_id"], source="pull", decision="valuable",
                                 content_ref=ref, content_hash=sha)
    _run(pg_env, "default", seed)
    with _pod(pg_env, tmp_path, monkeypatch, pod="registration-session-match") as client:
        client.app.state.services.extras["session_payloads"] = payloads
        _login(client, bootstrap=True)
        assert client.post(URL, headers=H, json=raw).status_code == 201
        before = _counts(pg_env)
        response = client.get(URL + "/match-diagnostic/session-match", params={"session_id": "observed-session"},
                              headers=H)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["matched"] is True
        assert result["eligible_for_evolution"] is False  # observed bytes alone do not establish source roots
        assert _counts(pg_env) == before
        assert client.get(URL + "/match-diagnostic/session-match", params={"session_id": "absent"},
                          headers=H).status_code == 404
