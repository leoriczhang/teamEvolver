"""An explicit administrator action authorizes one independent budget, never implicit renewal."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    record_format_guidance,
    request_guidance,
)
from team_miner.skill_miner.native_recovery import (
    prepare_explicit_retry,
)
from tests.miner.skill_miner.support.native_resource_recovery import failed_second_round


def authorization(job):
    return {"action": "retry_with_new_budget", "request_id": str(uuid.uuid4()),
            "expected_task_id": job["ov_task_id"], "budget_seconds": job["runtime_timeout_seconds"],
            "authorized_by": "reviewer"}


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name not in {"job.json", "run.log"}}




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
