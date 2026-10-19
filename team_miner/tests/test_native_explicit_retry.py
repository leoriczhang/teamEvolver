"""An explicit administrator action authorizes one independent budget, never implicit renewal."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from team_miner.native_compiler import AUXILIARY_FILES, record_format_guidance, request_guidance, retry_review_guidance
from team_miner.native_recovery import (
    effective_round_budget,
    ensure_round_budget,
    prepare_explicit_retry,
)
from team_miner.tests.test_native_mining_rounds import complete
from team_miner.tests.test_native_resource_recovery import failed_second_round
from team_miner.tests.test_ov_round_bridge import app_for


def authorization(job):
    return {"action": "retry_with_new_budget", "request_id": str(uuid.uuid4()),
            "expected_task_id": job["ov_task_id"], "budget_seconds": job["runtime_timeout_seconds"],
            "authorized_by": "reviewer"}


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name not in {"job.json", "run.log"}}


@pytest.mark.asyncio
async def test_admin_api_explicit_retry_keeps_old_round_and_grants_one_independent_attempt(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path, protocol_failure=True)
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    failed = repo.update_job(failed["job_id"], round_started_at=expired,
        native_repairs=[{"round": 2, "attempt": 1}, {"round": 2, "attempt": 2}])
    root = repo.jobs_root / failed["job_id"]
    original = hashes(root)
    public = repo.public_job(failed)
    assert public["can_resume"] is False and public["can_retry_native"] is True
    assert set(public["native_retry_context"]) == {"expected_task_id", "budget_seconds", "format_guidance_sha256"}
    auth = authorization(failed)
    body = {key: value for key, value in auth.items() if key != "authorized_by"}
    transport = httpx.ASGITransport(app=app_for(owner))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post(f"/api/mining/jobs/{failed['job_id']}/resume")).status_code == 409
        response = await client.post(f"/api/mining/jobs/{failed['job_id']}/resume", json=body)
        assert response.status_code == 200, response.text
        again = await client.post(f"/api/mining/jobs/{failed['job_id']}/resume", json=body)
        assert again.status_code == 200 and again.json()["job"]["native_explicit_retry"] == response.json()["job"][
            "native_explicit_retry"]
        assert (await client.post(f"/api/mining/jobs/{failed['job_id']}/resume",
            json={**body, "request_id": str(uuid.uuid4())})).status_code == 409
    retried = repo.get_job(failed["job_id"])
    retry = retried["native_explicit_retry"]
    assert retry["authorization"] == auth and retry["old_budget"]["started_at"] == expired
    assert retry["attempt_budget"]["seconds"] == failed["runtime_timeout_seconds"]
    assert retried["round_started_at"] == expired and retried["current_round"] == 2
    assert retried["native_repairs"] == failed["native_repairs"]
    assert retried["native_compile_history"] == failed["native_compile_history"]
    ensure_round_budget(retried)
    assert effective_round_budget(retried)[0] == retry["attempt_budget"]["started_at"]
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in original.items())
    with patch("team_miner.native_compiler.record_format_guidance", return_value="future software version"), patch(
            "team_miner.native_compiler.retry_review_guidance", return_value="future review version"):
        submitted = await owner._submit_ov_skill_round(retried)
    assert len(remote.compile_calls) == 3 and submitted["compile_request"]["from"] == failed["compile_request"]["from"]
    assert submitted["compile_request"]["to"] == failed["compile_request"]["to"]
    assert submitted["compile_request"]["skill"] == failed["compile_request"]["skill"]
    assert submitted["compile_request"]["instruction"].endswith(record_format_guidance() + retry_review_guidance())
    assert "future software version" not in submitted["compile_request"]["instruction"]
    assert "future review version" not in submitted["compile_request"]["instruction"]
    assert repo.public_job(submitted)["can_retry_native"] is False
    # An old original round deadline must not cancel a new, explicitly budgeted attempt.
    kept = await owner._ov_enforce_deadline(repo, submitted, "running")
    assert not kept.get("ov_cancel_reason") and not remote.cancelled
    complete(remote, submitted)
    await owner._refresh_ov_skill_job(submitted)
    assert repo.get_job(failed["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_explicit_retry_journal_exit_reentry_and_lost_acceptance_keep_one_authorization(tmp_path):
    owner, repo, remote, failed, task = await failed_second_round(tmp_path, protocol_failure=True)
    auth = authorization(failed)
    root = repo.jobs_root / failed["job_id"]
    receipt = prepare_explicit_retry(root, failed, authorization=auth, apply=True, remote_task=task)
    recovered = await owner._resume_native_resource_failure(failed["job_id"], auth)
    assert recovered["native_explicit_retry"] == receipt["retry"]
    assert await owner._resume_native_resource_failure(failed["job_id"], auth) == recovered
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(recovered)
    assert uncertain["status"] == "preparing" and len(remote.compile_calls) == 3
    frozen = uncertain["compile_request"]
    # Expired attempt still reconciles an already accepted task rather than resubmitting.
    retry = copy.deepcopy(uncertain["native_explicit_retry"])
    retry["attempt_budget"]["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    uncertain = repo.update_job(failed["job_id"], native_explicit_retry=retry)
    accepted = await owner._submit_ov_skill_round(uncertain)
    assert accepted["status"] == "queued" and accepted["compile_request"] == frozen
    assert len(remote.compile_calls) == 3 and len(accepted["native_compile_history"]) == 3


@pytest.mark.asyncio
async def test_explicit_retry_requires_matching_terminal_failure_and_frozen_sources(tmp_path):
    owner, repo, _, failed, task = await failed_second_round(tmp_path, protocol_failure=True)
    auth = authorization(failed)
    with pytest.raises(ValueError, match="准确远端任务"):
        repo.resume_native_failure(failed["job_id"], {**task, "status": "running"}, authorization=auth)
    with pytest.raises(ValueError, match="不是同一失败事件"):
        repo.resume_native_failure(failed["job_id"], {**task, "meta": {"pipeline": {"errors": [
            "reduce: schema: a different failure"]}}}, authorization=auth)
    for bad in [{**auth, "expected_task_id": "another-task"}, {**auth, "budget_seconds": auth["budget_seconds"] + 1},
                {**auth, "request_id": "not-a-uuid"}]:
        with pytest.raises(ValueError):
            await owner._resume_native_resource_failure(failed["job_id"], bad)
    root = repo.jobs_root / failed["job_id"]
    manifest = json.loads((root / "round_inputs/round-2/manifest.json").read_text())
    source = __import__("pathlib").Path(manifest[0]["local_path"])
    source.write_text(source.read_text() + "changed", encoding="utf-8")
    with pytest.raises(ValueError, match="完整性校验失败"):
        await owner._resume_native_resource_failure(failed["job_id"], auth)


@pytest.mark.asyncio
async def test_explicit_retry_rejects_viewer_and_non_protocol_failure(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path, protocol_failure=True)
    auth = authorization(failed)
    body = {key: value for key, value in auth.items() if key != "authorized_by"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(owner, admin=False)),
                                 base_url="http://test") as client:
        assert (await client.post(f"/api/mining/jobs/{failed['job_id']}/resume", json=body)).status_code == 403
    other = {**failed, "native_failure": {}, "error": {"code": "NETWORK_ERROR"}, "ov_pipeline": {"errors": []}}
    with pytest.raises(ValueError, match="仅适用于明确"):
        prepare_explicit_retry(repo.jobs_root / failed["job_id"], other, authorization=auth)
    assert len(remote.compile_calls) == 2 and not repo.get_job(failed["job_id"]).get("native_explicit_retry")


@pytest.mark.asyncio
async def test_explicit_retry_cannot_reuse_format_guidance_already_in_accepted_request(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path, protocol_failure=True,
        old_guidance=request_guidance(AUXILIARY_FILES) + record_format_guidance())
    assert repo.public_job(failed)["can_retry_native"] is False
    with pytest.raises(ValueError, match="格式指导已执行过"):
        await owner._resume_native_resource_failure(failed["job_id"], authorization(failed))
    assert len(remote.compile_calls) == 2 and not failed.get("native_explicit_retry")


@pytest.mark.asyncio
async def test_explicit_retry_attempt_expiry_never_renews_or_posts(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path, protocol_failure=True)
    recovered = await owner._resume_native_resource_failure(failed["job_id"], authorization(failed))
    root = repo.jobs_root / failed["job_id"]
    retry = copy.deepcopy(recovered["native_explicit_retry"])
    retry["attempt_budget"]["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    # Keep the audited budget and mutable projection identical while simulating elapsed time.
    path = next((root / "round_inputs/round-2/native-explicit-retry").glob("*.json"))
    receipt = json.loads(path.read_text())
    receipt["retry"] = retry
    path.write_text(json.dumps(receipt), encoding="utf-8")
    recovered = repo.update_job(failed["job_id"], native_explicit_retry=retry)
    rejected = await owner._submit_ov_skill_round(recovered)
    assert rejected["status"] == "failed" and "预算已到期" in rejected["error"]
    assert len(remote.compile_calls) == 2
