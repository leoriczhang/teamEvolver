"""Runtime dependency failures do not become more content-generation requests."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_failures import classify_native_failure
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

COMPILER = "viking://agent/skills/frozen-compiler"
SOURCE = "viking://resources/frozen/source"


@pytest.mark.parametrize(("task", "expected"), [
    ({"error": "File not found: " + COMPILER + "/references/unknown-resource.md"}, "compiler_dependency"),
    ({"error": "File not found: " + SOURCE + "/original.md"}, "source_missing"),
    ({"error": {"code": "SOURCE_NOT_FOUND"}}, "source_missing"),
    ({"error": {"code": "UNAUTHORIZED"}}, "auth"),
    ({"error": "HTTP 403 permission denied"}, "auth"),
    ({"error": {"code": "NETWORK_ERROR"}}, "transport"),
    ({"error": "All connection attempts failed"}, "transport"),
    ({"error": {"code": "SCHEMA_VALIDATION_FAILED"}}, "artifact_validation"),
    ({"error": {"code": "OUTPUT_TRUNCATED"}}, "output_truncated"),
    ({"error": "map: schema: records.1.inputs: missing: Field required"}, "output_protocol"),
    ({"error": "reduce: format: Expected one emit call"}, "output_protocol"),
    ({"error": "map: semantic: Input references must name supplied inputs"}, "unknown"),
    ({"error": "records.1.inputs: missing: Field required"}, "unknown"),
    ({"error": "map: schema: invalid output; map: semantic: Unknown supporting input"}, "unknown"),
    ({"error": "COMPILE_INCOMPLETE: no accepted final artifacts"}, "unknown"),
    ({"error": "schema or source is probably wrong; retry the whole task"}, "unknown"),
    ({"error": "File not found: viking://resources/unclassified/file.md"}, "unknown"),
])
def test_remote_failure_classification_requires_specific_evidence(task, expected):
    failure = classify_native_failure(task, compiler_uri=COMPILER, source_uris=[SOURCE])
    assert failure["kind"] == expected
    assert failure["repairable"] == (expected in {"artifact_validation", "output_truncated"})
    assert failure["diagnostics"]["error"] == task["error"]


def test_missing_dependency_takes_precedence_over_truncation_and_retains_events():
    missing = COMPILER + "/resources/missing-table.json"
    task = {"task_id": "cmp-real-failure", "status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
            "meta": {"pipeline": {"errors": ["File not found: " + missing,
                "Agent output was truncated; use smaller writes or content_ref and complete tool calls"]}},
            "execution_events": {"items": [{"event": "pipeline_failed"}]}}
    failure = classify_native_failure(task, compiler_uri=COMPILER)
    assert failure["kind"] == "compiler_dependency" and not failure["repairable"]
    assert failure["dependency_uris"] == [missing]
    assert failure["diagnostics"]["pipeline"] == task["meta"]["pipeline"]
    assert failure["diagnostics"]["execution_events"] == task["execution_events"]
    assert classify_native_failure(task, compiler_uri=COMPILER) == failure


def test_local_contract_error_is_explicit_and_retains_original_diagnostic():
    detail = "案例引用无效，viking://agent/skills/other/private.md，必须保留现有来源边界。"
    failure = classify_native_failure({"error": detail}, local_validation_detail=detail)
    assert failure["kind"] == "artifact_validation"
    assert failure["diagnostics"]["origin"] == "local_artifact_contract"
    assert failure["diagnostics"]["error"] == detail
    assert failure["diagnostics"]["validation_detail"] == detail


@pytest.mark.asyncio
async def test_228_missing_dependencies_stop_without_compile_and_preserve_first_round(tmp_path):
    owner, repo, remote, first = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(first)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(first["job_id"])
    second = repo.prepare_next_round(first["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "unresolved-review", submitted_by="admin")
    second = await owner._submit_ov_skill_round(second)
    root = repo.jobs_root / first["job_id"]

    def hashes(directory):
        return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()}

    archived, current = hashes(root / "rounds/round-1"), hashes(repo.workspace(second))
    request = dict(second["compile_request"])
    missing = second["compiler_uri"] + "/references/scope-fields.md"
    real_error = "228 pipeline jobs failed: " + repr(["File not found: " + missing] * 228)
    task = {"status": "failed", "error": {"code": "COMPILE_INCOMPLETE",
            "message": "No accepted final artifacts remain after failed jobs"},
            "meta": {"pipeline": {"errors": [real_error,
                "No accepted final artifacts remain after failed jobs"]}},
            "execution_events": {"items": [{"event": "pipeline_failed", "count": 228}]}}
    remote.tasks[second["ov_task_id"]] = task
    await owner._refresh_ov_skill_job(second)
    failed = repo.get_job(first["job_id"])
    assert failed["status"] == "failed" and failed["native_failure"]["kind"] == "compiler_dependency"
    assert failed["native_failure"]["dependency_uris"] == [missing]
    assert failed["native_failure"]["diagnostics"]["pipeline"]["errors"][0] == real_error
    assert failed["compile_request"] == request and failed["ov_task_id"] == second["ov_task_id"]
    assert not failed.get("native_repairs") and failed["native_repair_instruction"] == ""
    assert hashes(root / "rounds/round-1") == archived and hashes(repo.workspace(failed)) == current
    archive = repo.project_root / failed["native_failure"]["archive"]
    saved = json.loads(archive.read_text())
    assert saved["diagnostics"]["execution_events"] == task["execution_events"]
    await owner._refresh_ov_skill_job(failed)
    assert len(remote.compile_calls) == 2 and len(list(archive.parent.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_truncation_keeps_original_failure_without_another_compile(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    raw_error = "Agent output was truncated; use smaller writes or content_ref and complete tool calls"
    remote.tasks[job["ov_task_id"]] = {"status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
        "meta": {"pipeline": {"errors": [raw_error]}}}
    await owner._refresh_ov_skill_job(job)
    failed = repo.get_job(job["job_id"])
    assert failed["status"] == "failed" and failed["native_failure"]["kind"] == "output_truncated"
    assert failed["native_failure"]["diagnostics"]["pipeline"]["errors"] == [raw_error]
    assert not failed.get("native_repairs") and failed["native_repair_instruction"] == ""
    assert failed["compile_request"] == job["compile_request"] and failed["ov_task_id"] == job["ov_task_id"]
    await owner._refresh_ov_skill_job(failed)
    assert len(remote.compile_calls) == 1
