"""Temporary resource contention must not change a frozen request or extend its budget."""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.knowledge_compiler import SKILL_BODY
from tests.miner.skill_miner.support.native_mining_rounds import new_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 423])
async def test_resource_lock_before_submission_retries_same_identity_within_original_budget(
    tmp_path, monkeypatch, status,
):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    original = owner._round_provision_compiler
    calls = 0

    async def conflict_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPException(status, "lock acquire timed out after 0ms")
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, "_round_provision_compiler", conflict_once)
    pending = await owner._submit_ov_skill_round(job)
    assert pending["status"] == "preparing" and not pending["ov_task_id"]
    assert pending["ov_next_retry_at"] and pending["submission_error"]
    assert not remote.compile_calls
    start, key = pending["round_started_at"], pending["submission_idempotency_key"]
    accepted = await owner._submit_ov_skill_round(pending)
    assert accepted["status"] == "queued" and len(remote.compile_calls) == 1
    assert accepted["round_started_at"] == start and accepted["submission_idempotency_key"] == key
    assert len(accepted["native_compile_history"]) == 1


@pytest.mark.asyncio
async def test_persistent_resource_lock_cannot_restart_an_expired_preparation_budget(tmp_path, monkeypatch):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")

    async def conflict(*args, **kwargs):
        raise HTTPException(409, "lock acquire timed out after 0ms")

    monkeypatch.setattr(owner, "_round_provision_compiler", conflict)
    pending = await owner._submit_ov_skill_round(job)
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    pending = repo.update_job(pending["job_id"], round_started_at=expired)
    result = await owner._submit_ov_skill_round(pending)
    assert result["status"] == "failed" and result["round_started_at"] == expired
    assert not remote.compile_calls and not result["ov_task_id"]


@pytest.mark.asyncio
async def test_unrelated_conflict_is_not_retried_or_relaxed(tmp_path, monkeypatch):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")

    async def conflict(*args, **kwargs):
        raise HTTPException(409, "Idempotency key already belongs to a different request")

    monkeypatch.setattr(owner, "_round_provision_compiler", conflict)
    result = await owner._submit_ov_skill_round(job)
    assert result["status"] == "failed" and not remote.compile_calls


@pytest.mark.asyncio
async def test_lock_response_after_acceptance_reconciles_without_another_post(tmp_path, monkeypatch):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    remote.lookup_supported = True
    original = owner._knowledge_request

    async def response_lost(account, method, path, **kwargs):
        result = await original(account, method, path, **kwargs)
        if method == "POST" and path == "/api/v1/compile":
            raise HTTPException(409, "lock acquire timed out after 0ms")
        return result

    monkeypatch.setattr(owner, "_knowledge_request", response_lost)
    pending = await owner._submit_ov_skill_round(job)
    assert pending["status"] == "preparing" and len(remote.compile_calls) == 1
    recovered = await owner._submit_ov_skill_round(pending)
    assert recovered["status"] == "queued" and recovered["ov_task_id"]
    assert len(remote.compile_calls) == 1 and len(recovered["native_compile_history"]) == 1
