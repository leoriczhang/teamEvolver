"""Unarchived ingestion diagnostics compose domain and platform public reads."""
from __future__ import annotations

import pytest

from session_ingestion.api import create_router
from session_ingestion.payloads import SessionPayloads
from session_ingestion.repository import SessionIngestionRepository
from teamEvolver.runtime.repository import WorkerTaskRepository
from tests.support.app import H, _login, _pod
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def _diagnostic_pod(env, tmp_path, monkeypatch, *, pod):
    client = _pod(env, tmp_path, monkeypatch, pod=pod)
    services = client.app.state.services
    # The shared Skill-only API fixture omits the ingestion payload contract.
    # Mount this module with its real PG payload store before entering TestClient.
    services.extras["session_payloads"] = SessionPayloads(env.tx)
    client.app.include_router(create_router(services))
    return client


def _seed(env, *, tenant="default", identifier="source-revision", status="processing", jobs=None):
    async def seed(conn):
        ingestions = SessionIngestionRepository(env.tx)
        await ingestions.accept(
            conn, tenant_id=tenant, ingestion_id=identifier, source="pull", external_id="upstream-session",
            task_id=f"{identifier}:session.normalize", payload_ref="private-raw-ref", payload_hash="raw-hash",
        )
        await ingestions.set_status(conn, ingestion_id=identifier, status=status)
        await ingestions.record_stage(
            conn, tenant_id=tenant, ingestion_id=identifier, stage="trace_analyze", status="failed",
            error_code="ValueError", detail={"private": "not a public diagnostic"}, result_ref="private-stage-ref",
        )
        tasks = WorkerTaskRepository(env.tx)
        for kind, state in (jobs or {}).items():
            task_id = f"{identifier}:session.{kind}"
            await tasks.enqueue(
                conn, tenant_id=tenant, task_id=task_id, kind=f"session.{kind}",
                subject_type="ingestion", subject_id=identifier, payload_ref="private-task-ref",
            )
            await conn.execute(
                f"UPDATE {env.schema}.worker_tasks SET status=$2,attempt=3,error_code=$3 WHERE task_id=$1",
                task_id, state, "retries_exhausted" if state == "failed" else "",
            )
    _run(env, tenant, seed)
    return identifier


def test_failed_analysis_before_archive_is_queryable_without_mutating_raw_state(pg_env, tmp_path, monkeypatch):
    identifier = _seed(pg_env, jobs={"normalize": "succeeded", "analyze": "failed"})
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-diagnostics") as client:
        _login(client, bootstrap=True)
        assert client.get(f"/api/sessions/session_{identifier}/stages", headers=H).status_code == 404
        response = client.get(f"/api/ingestions/{identifier}", headers=H)
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["ingestion_status"] == "processing"
        assert detail["effective_status"] == "failed"
        assert detail["current_job"]["job_id"] == f"{identifier}:session.analyze"
        assert detail["current_job"]["status"] == "failed"
        assert detail["current_job"]["attempt"] == 3
        assert detail["current_job"]["error_code"] == "retries_exhausted"
        assert detail["stages"][0]["stage"] == "trace_analyze"
        assert detail["stages"][0]["error_code"] == "ValueError"
        assert set(detail["current_job"]) == {
            "job_id", "kind", "status", "attempt", "error_code", "created_at", "updated_at",
        }
        assert set(detail["stages"][0]) == {"stage", "status", "error_code", "created_at", "updated_at"}
        assert "private-" not in response.text
    saved = _run(pg_env, "default", lambda conn: SessionIngestionRepository(pg_env.tx).get_ingestion(
        conn, ingestion_id=identifier,
    ))
    assert saved["status"] == "processing"


def test_retry_same_failed_job_projects_processing_despite_old_failed_stage(pg_env, tmp_path, monkeypatch):
    identifier = _seed(pg_env, jobs={"normalize": "succeeded", "analyze": "failed"})
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-retry") as client:
        _login(client, bootstrap=True)
        job_id = f"{identifier}:session.analyze"
        retry = client.post(f"/api/jobs/{job_id}/retry", headers=H)
        assert retry.status_code == 200, retry.text
        detail = client.get(f"/api/ingestions/{identifier}", headers=H).json()
        assert detail["ingestion_status"] == "processing"
        assert detail["effective_status"] == "processing"
        assert detail["current_job"]["job_id"] == job_id
        assert detail["current_job"]["status"] == "queued"
        assert detail["stages"][0]["status"] == "failed"


@pytest.mark.parametrize("status", ["archived", "skipped"])
def test_completed_business_status_is_preserved_with_failed_worker_confirmation(
    pg_env, tmp_path, monkeypatch, status,
):
    identifier = _seed(pg_env, status=status, jobs={"analyze": "succeeded", "archive": "failed"})
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-archived") as client:
        _login(client, bootstrap=True)
        detail = client.get(f"/api/ingestions/{identifier}", headers=H).json()
        assert detail["ingestion_status"] == status
        assert detail["effective_status"] == status
        assert detail["current_job"]["status"] == "failed"
        assert detail["current_job"]["kind"] == "session.archive"


def test_other_tenant_ingestion_and_unknown_identifier_are_not_disclosed(pg_env, tmp_path, monkeypatch):
    identifier = _seed(pg_env, tenant="alpha", jobs={"analyze": "failed"})
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-tenant") as client:
        _login(client, bootstrap=True)
        for path in (identifier, "never-accepted"):
            response = client.get(f"/api/ingestions/{path}", headers=H)
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": "Ingestion not found"}
        own = client.get(f"/api/ingestions/{identifier}", headers={"X-Tenant-ID": "alpha"})
        assert own.status_code == 200, own.text
        assert own.json()["effective_status"] == "failed"


@pytest.mark.parametrize("state", ["running", "retry_wait", "waiting_external", "reconciling", "cancelled"])
def test_highest_existing_pipeline_task_controls_projection(pg_env, tmp_path, monkeypatch, state):
    identifier = _seed(pg_env, jobs={"normalize": "succeeded", "analyze": "failed", "archive": state})
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-active-stage") as client:
        _login(client, bootstrap=True)
        detail = client.get(f"/api/ingestions/{identifier}", headers=H).json()
        assert detail["current_job"]["kind"] == "session.archive"
        assert detail["effective_status"] == ("cancelled" if state == "cancelled" else "processing")


def test_accepted_ingestion_without_task_keeps_raw_state(pg_env, tmp_path, monkeypatch):
    identifier = _seed(pg_env, status="accepted")
    with _diagnostic_pod(pg_env, tmp_path, monkeypatch, pod="ingestion-no-task") as client:
        _login(client, bootstrap=True)
        detail = client.get(f"/api/ingestions/{identifier}", headers=H).json()
        assert detail["ingestion_status"] == detail["effective_status"] == "accepted"
        assert detail["current_job"] is None
