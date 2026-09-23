import logging

import httpx
import pytest
from fastapi import HTTPException

from team_ontology.config import OntologyConfig
from team_ontology.diagnostics import FailureSummary
from team_ontology.service import Operations
from teamEvolver.logging_runtime import SafeFormatter, log_context


async def resolver(principal):
    return {
        "api_key": "a-private-root-key",
        "url": "http://ov.test",
        "account": principal["tenant"],
        "subject": "mapped-user",
        "model": {},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,detail,expected",
    [
        (404, "Not Found", "OV_NOT_FOUND"),
        (403, "ONTOLOGY_DISABLED", "ONTOLOGY_DISABLED"),
        (403, "FORBIDDEN", "FORBIDDEN"),
    ],
)
async def test_upstream_error_diagnostics(status, detail, expected, caplog):
    async def response(request):
        assert request.headers["x-request-id"] == "trace-123"
        return httpx.Response(status, json={"detail": detail}, headers={"x-request-id": "ov-123"})

    ops = Operations(OntologyConfig(), "postgresql://unused", resolver, httpx.MockTransport(response))
    caplog.set_level(logging.DEBUG)
    with log_context(request_id="trace-123"):
        with pytest.raises(HTTPException) as error:
            await ops.ov({"tenant": f"tenant-{expected}", "subject": "frank"}, "GET", "/ontology/capabilities")
    assert error.value.status_code == status
    metadata = [r.fields for r in caplog.records if getattr(r, "event", "") == "ontology.failure"]
    record = next(r for r in metadata if r["code"] == expected)
    assert record["ov_account"] == f"tenant-{expected}" and record["ov_user"] == "mapped-user"
    assert record["ov_request_id"] == "ov-123" and record["status"] == status
    assert "a-private-root-key" not in "\n".join(SafeFormatter().format(r) for r in caplog.records)


@pytest.mark.asyncio
async def test_timeout_identity_mismatch_and_query_redaction(caplog):
    caplog.set_level(logging.DEBUG)

    def timeout(request):
        raise httpx.ReadTimeout("upstream timeout")

    ops = Operations(OntologyConfig(), "postgresql://unused", resolver, httpx.MockTransport(timeout))
    with pytest.raises(HTTPException):
        await ops.ov({"tenant": "timeout", "subject": "frank"}, "GET", "/assets/commits/by-key?key=private")
    assert any(getattr(r, "fields", {}).get("code") == "OV_TIMEOUT" for r in caplog.records)
    assert not any("?" in str(getattr(r, "fields", {}).get("path", "")) for r in caplog.records)
    ops.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"tenant": "wrong", "subject": "wrong"}))
    with pytest.raises(HTTPException, match="OV_CREDENTIAL_IDENTITY_MISMATCH"):
        await ops.ov({"tenant": "identity", "subject": "frank"}, "GET", "/ontology/capabilities")
    assert any(getattr(r, "event", "") == "ontology.identity_mismatch" for r in caplog.records)


def test_failure_summary_first_change_recovery(caplog):
    caplog.set_level(logging.DEBUG)
    summary = FailureSummary()
    key = ("tenant", "frank", "phase")
    summary.failure(key, "FIRST")
    summary.failure(key, "FIRST")
    summary.failure(key, "SECOND")
    summary.failure(key, "SECOND")
    summary.success(key)
    events = [r for r in caplog.records if getattr(r, "event", "").startswith("ontology.")]
    assert len(events) == 3
    assert events[1].fields["suppressed"] == 1
    assert events[2].event == "ontology.recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status,expected", [
    ({"error": {"code": "INVALID_ARGUMENT", "message":
        "Directory URI is not readable as a file: viking://resources/private-source. List it first."}},
     400, "SOURCE_URI_IS_DIRECTORY"),
    ({"error": {"code": "INVALID_ARGUMENT", "message": "secret business payload"}}, 400, "INVALID_ARGUMENT"),
    ({"error": {"code": "FORBIDDEN", "message": "secret business payload"}}, 403, "FORBIDDEN"),
    ({"detail": {"code": "SOURCE_REVOKED", "input": "secret business payload"}}, 403, "SOURCE_REVOKED"),
    ({"detail": [{"loc": ["body"], "input": "secret business payload"}]}, 422, "OV_VALIDATION_ERROR"),
    (["secret business payload"], 502, "OV_REQUEST_FAILED"),
])
async def test_native_and_enterprise_errors_are_safe_and_actionable(payload, status, expected, caplog):
    ops = Operations(OntologyConfig(), "postgresql://unused", resolver, httpx.MockTransport(
        lambda _: httpx.Response(status, json=payload)))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as error:
        await ops.ov({"tenant": f"native-{expected}", "subject": "frank"}, "POST", "/snapshots/freeze")
    assert error.value.detail == expected and error.value.status_code == status
    output = "\n".join(SafeFormatter().format(r) for r in caplog.records)
    assert "secret business payload" not in output and "private-source" not in output
    assert expected in output


@pytest.mark.asyncio
async def test_directory_error_classification_is_limited_to_freeze():
    ops = Operations(OntologyConfig(), "postgresql://unused", resolver, httpx.MockTransport(
        lambda _: httpx.Response(400, json={"error": {"code": "INVALID_ARGUMENT",
            "message": "Directory URI is not readable as a file: viking://resources/example"}})))
    with pytest.raises(HTTPException) as error:
        await ops.ov({"tenant": "t", "subject": "frank"}, "POST", "/artifacts")
    assert error.value.detail == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_build_failure_metadata_does_not_log_candidate_or_body(tmp_path, caplog):
    ops = Operations(OntologyConfig(state_dir=str(tmp_path)), "postgresql://unused", resolver)

    class Store:
        async def finish(self, job, **kwargs):
            assert kwargs["error"] == "ValueError"

    ops.store = Store()

    async def broken(*args):
        raise ValueError("a secret source business paragraph")

    ops.ov = broken
    caplog.set_level(logging.DEBUG)
    await ops.execute({"id": "ont_test", "tenant": "t", "subject": "frank", "attempt": 2})
    text = "\n".join(SafeFormatter().format(r) for r in caplog.records)
    assert "ontology.execution_failed" in text and "ont_test" in text and "attempt" in text
    assert "business paragraph" not in text
