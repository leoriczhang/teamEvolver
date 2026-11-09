"""Safe analysis metadata reaches stage/Worker failure without archiving or a business import."""

from __future__ import annotations

import json

import pytest

from session_ingestion.diagnostics import ANALYSIS_DIAGNOSTIC_CODES
from session_ingestion.handlers import KIND_ANALYZE, KIND_ARCHIVE
from session_ingestion.repository import SessionIngestionRepository
from teamEvolver.runtime import process_once
from tests.support.ingestion import _accept_push, _drain, _StageRunner, _worker_services

PRIVATE = "PRIVATE-provider-key-and-source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "session_analysis.llm_unconfigured",
        "session_analysis.provider.TimeoutError",
        "session_analysis.missing.classification",
        "session_analysis.missing.summary",
        "session_analysis.missing.judge",
        "session_analysis.missing.classification+summary+judge",
        "session_analysis.provider.OtherError",
        PRIVATE,
        {"secret": PRIVATE},
        None,
    ],
)
async def test_fixed_codes_reach_stage_and_worker_while_unsafe_attributes_keep_legacy_behavior(tx, db, payloads, code):
    class AnalysisFailure(RuntimeError):
        pass

    class Runner(_StageRunner):
        async def analyze(self, session, *, payload):
            error = AnalysisFailure(PRIVATE)
            error.diagnostic_code = code
            raise error

    iid, _ = await _accept_push(tx, payloads)
    services = _worker_services(tx, payloads, runner=Runner())
    expected = code if isinstance(code, str) and code in ANALYSIS_DIAGNOSTIC_CODES else "AnalysisFailure"
    for _ in range(3):  # Normalize, Split, then the first failing Analyze attempt.
        await process_once(services, owner_id="diagnostic-worker", tenants=("default",), rand=lambda: 0.0)
    first = db.tasks["default", f"{iid}:{KIND_ANALYZE}"]
    assert first["status"] == "retry_wait" and first["attempt"] == 1
    assert first["error_code"] == "analyze_failed:" + expected
    await _drain(services)
    async with tx.tenant_tx("default") as conn:
        rows = await SessionIngestionRepository(tx).stage_results(conn, ingestion_id=iid)
    failed = next(row for row in rows if row["stage"] == "analyze")
    assert failed["status"] == "failed" and failed["error_code"] == expected
    task = db.tasks["default", f"{iid}:{KIND_ANALYZE}"]
    assert task["status"] == "failed" and task["attempt"] == 3
    # Runtime may replace its final error with retries_exhausted; the stage keeps the precise code.
    assert PRIVATE not in json.dumps({"stages": rows, "task_error": task["error_code"]}, default=str)
    assert ("default", f"{iid}:{KIND_ARCHIVE}") not in db.tasks
    assert not db.sessions
