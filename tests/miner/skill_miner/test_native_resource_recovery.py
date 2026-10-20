"""Regression for model-selected missing attachments and immutable compiler recovery."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    RESOURCE_POLICY,
    SKILL_BODY,
    request_guidance,
    resource_policy,
)
from team_miner.skill_miner.native_recovery import pending_guidance, prepare_recovery
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner


def test_resource_inventory_is_complete_and_inline_fields_are_not_paths():
    guide = resource_policy(AUXILIARY_FILES)
    assert json.dumps(sorted(AUXILIARY_FILES)) in guide
    assert "scope-fields.md" not in guide
    assert "scope_fields" in guide and "空对象表示没有声明该类字段" in guide
    assert RESOURCE_POLICY not in SKILL_BODY  # The host declares resources exactly once, in the request.
    other = resource_policy({"references/different.md": "different"})
    assert '"references/different.md"' in other
    assert '"references/output-contract.md"' not in other


async def failed_second_round(tmp_path, *, old_guidance="", protocol_failure=False):
    owner, repo, remote, job = new_owner(tmp_path)
    root = repo.jobs_root / job["job_id"]
    legacy = SKILL_BODY.split("\n## 编译 Skill 附件边界\n", 1)[0]
    (root / "compiler-skill.md").write_text(legacy, encoding="utf-8")
    # Reproduce a historical request accepted before the TE resource policy existed.
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=old_guidance):
        first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "review-unresolved", submitted_by="admin")
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=old_guidance):
        second = await owner._submit_ov_skill_round(second)
    error = ("map: schema: 1 validation error for EmitRecords records.1.inputs Field required"
             if protocol_failure else "File not found: " + second["compiler_uri"] + "/references/unknown.md")
    task = {"task_id": second["ov_task_id"], "status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
            "meta": {"pipeline": {"errors": [error]}}}
    remote.tasks[second["ov_task_id"]] = task
    await owner._refresh_ov_skill_job(second)
    return owner, repo, remote, repo.get_job(job["job_id"]), task


@pytest.mark.asyncio
async def test_recovery_keeps_compiler_first_round_requests_and_retry_history(tmp_path):
    owner, repo, remote, failed, task = await failed_second_round(tmp_path)
    root = repo.jobs_root / failed["job_id"]
    before = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file() and p.name not in {"job.json", "run.log"}}
    assert repo.public_job(failed)["can_resume"]
    assert len(remote.compile_calls) == 2
    with pytest.raises(ValueError, match="准确远端任务"):
        repo.resume_native_failure(failed["job_id"], {**task, "status": "running"})
    journal = prepare_recovery(root, failed, apply=True, remote_task=task)
    assert journal["budget_started_at"] == failed["round_started_at"]
    # Simulate an exit after journal commit but before updating the mutable job state.
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    assert recovered["submission_idempotency_key"] == journal["submission_key"]
    assert recovered["status"] == "preparing"
    assert recovered.get("native_repairs") == failed.get("native_repairs")
    assert recovered["checkpoint_history"] == failed["checkpoint_history"]
    assert recovered["native_compile_history"] == failed["native_compile_history"]
    assert await owner._resume_native_resource_failure(failed["job_id"]) == recovered
    submitted = await owner._submit_ov_skill_round(recovered)
    assert len(remote.compile_calls) == 3
    assert submitted["compile_request"]["from"] == failed["compile_request"]["from"]
    assert submitted["compile_request"]["to"] == failed["compile_request"]["to"]
    assert submitted["compiler_uri"] == failed["compiler_uri"]
    assert submitted["compiler_sha256"] == failed["compiler_sha256"]
    assert RESOURCE_POLICY in submitted["compile_request"]["instruction"]
    assert len(submitted["native_contract_recoveries"]) == 1
    assert "unknown.md" not in submitted["compile_request"]["instruction"]
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    complete(remote, submitted)
    await owner._refresh_ov_skill_job(submitted)
    assert repo.get_job(failed["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_recovery_rejects_tampered_original_inputs_and_repeated_policy_revision(tmp_path):
    owner, repo, _, failed, task = await failed_second_round(tmp_path)
    root = repo.jobs_root / failed["job_id"]
    manifest = json.loads((root / "round_inputs/round-2/manifest.json").read_text())
    source = Path(manifest[0]["local_path"])
    original = source.read_bytes()
    source.write_bytes(original + b"changed")
    with pytest.raises(ValueError, match="完整性校验失败"):
        repo.resume_native_failure(failed["job_id"], task)
    source.write_bytes(original)
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    failed_again = {**recovered, "status": "failed", "ov_status": "failed"}
    with pytest.raises(ValueError, match="不一致"):
        prepare_recovery(root, failed_again)


@pytest.mark.asyncio
async def test_recovery_rejects_changed_remote_dependency_and_declared_resource_missing(tmp_path):
    _, repo, _, failed, task = await failed_second_round(tmp_path)
    with pytest.raises(ValueError, match="不是同一失败事件"):
        repo.resume_native_failure(failed["job_id"], {**task, "meta": {"pipeline": {"errors": [
            "File not found: " + failed["compiler_uri"] + "/references/different.md"]}}})
    root = repo.jobs_root / failed["job_id"]
    different = {**failed, "native_failure": {}, "ov_pipeline": {"errors": [
        "File not found: " + failed["compiler_uri"] + "/references/output-contract.md"]}, "error": "missing"}
    with pytest.raises(ValueError, match="真实依赖"):
        prepare_recovery(root, different)
    mixed = {**failed, "native_failure": {}, "ov_pipeline": {"errors": [
        "File not found: " + failed["compiler_uri"] + "/references/unknown.md",
        "File not found: " + failed["source_snapshot"] + "/original/policy.md"]}}
    with pytest.raises(ValueError, match="真实依赖"):
        prepare_recovery(root, mixed)


@pytest.mark.asyncio
async def test_recovery_rejects_changed_committed_journal(tmp_path):
    _, repo, _, failed, task = await failed_second_round(tmp_path)
    root = repo.jobs_root / failed["job_id"]
    receipt = prepare_recovery(root, failed, apply=True, remote_task=task)
    receipt["submission_key"] = "different-request"
    path = next((root / "round_inputs/round-2/native-recoveries").glob("*.json"))
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="恢复事件不一致"):
        repo.resume_native_failure(failed["job_id"], task)


@pytest.mark.asyncio
async def test_recovery_lost_acceptance_replays_identical_request_without_new_compile(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path)
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(recovered)
    assert uncertain["status"] == "preparing" and not uncertain["ov_task_id"]
    frozen = uncertain["compile_request"]
    resumed = await owner._submit_ov_skill_round(uncertain)
    assert resumed["status"] == "queued" and resumed["compile_request"] == frozen
    assert len(remote.compile_calls) == 3
    assert len(resumed["native_compile_history"]) == 3
    assert repo.workspace(resumed).joinpath("compiled_skill/policy-review/SKILL.md").is_file()


@pytest.mark.asyncio
async def test_resource_upgrade_preserves_legacy_budget_sources_and_counts(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path)
    budget_start = failed["round_started_at"]
    old_legacy = {"round": 2, "protocol": "te-native-resource-recovery.v1", "submission_key": "old-resource-upgrade"}
    old_repairs = [{"round": 2, "attempt": 1, "error": "archived first repair"},
                   {"round": 2, "attempt": 2, "error": "archived second repair"}]
    failed = repo.update_job(failed["job_id"], native_resource_recovery=old_legacy, native_repairs=old_repairs)
    root = repo.jobs_root / failed["job_id"]
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file() and p.name not in {"job.json", "run.log"}}
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    assert recovered["round_started_at"] == budget_start
    assert recovered["native_resource_recovery"] == old_legacy and recovered["native_repairs"] == old_repairs
    assert recovered["native_contract_recoveries"][0]["upgrade_index"] == 2
    submitted = await owner._submit_ov_skill_round(recovered)
    assert submitted["compile_request"]["from"] == failed["compile_request"]["from"]
    assert submitted["compile_request"]["to"] == failed["compile_request"]["to"]
    assert submitted["compile_request"]["skill"] == failed["compile_request"]["skill"]
    assert submitted["compile_request"]["instruction"].count(RESOURCE_POLICY) == 1
    assert len(remote.compile_calls) == 3
    assert all(hashlib.sha256((root / path).read_bytes()).hexdigest() == digest for path, digest in before.items())
    assert (repo.public_job(submitted)["last_checkpoint_submission"]
            == repo.public_job(failed)["last_checkpoint_submission"])


@pytest.mark.asyncio
async def test_same_guidance_cannot_retry_and_version_upgrades_have_total_bound(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path)
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    first_budget = recovered["round_started_at"]
    submitted = await owner._submit_ov_skill_round(recovered)

    async def fail_dependency(job):
        task = {"task_id": job["ov_task_id"], "status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
                "meta": {"pipeline": {"errors": [
                    "File not found: " + job["compiler_uri"] + "/references/unknown.md"]}}}
        remote.tasks[job["ov_task_id"]] = task
        await owner._refresh_ov_skill_job(job)
        return repo.get_job(job["job_id"]), task

    failed_again, task = await fail_dependency(submitted)
    with pytest.raises(ValueError, match="同一契约指导版本"):
        repo.resume_native_failure(failed_again["job_id"], task)
    newer = request_guidance(AUXILIARY_FILES).replace(RESOURCE_POLICY, "te-declared-skill-resources.v2").replace(
        "不得从字段名", "先核对完整附件清单；不得从字段名")
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=newer):
        recovered_again = await owner._resume_native_resource_failure(failed_again["job_id"])
        assert recovered_again["round_started_at"] == first_budget
        assert len(recovered_again["native_contract_recoveries"]) == 2
        next_task = await owner._submit_ov_skill_round(recovered_again)
    third_failure, task = await fail_dependency(next_task)
    newest = newer.replace("te-declared-skill-resources.v2", "te-declared-skill-resources.v3")
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=newest):
        with pytest.raises(ValueError, match="两次上限"):
            repo.resume_native_failure(third_failure["job_id"], task)
    assert len(remote.compile_calls) == 4


@pytest.mark.asyncio
async def test_resource_upgrade_rejects_changed_remote_error_event(tmp_path):
    _, repo, _, failed, task = await failed_second_round(tmp_path)
    changed = {**task, "error": {"code": "OTHER_FAILURE"}}
    with pytest.raises(ValueError, match="不是同一失败事件"):
        repo.resume_native_failure(failed["job_id"], changed)
    changed = {**task, "meta": {"pipeline": {"errors": ["reduce: schema: another rejected record"]}}}
    with pytest.raises(ValueError, match="不是同一失败事件"):
        repo.resume_native_failure(failed["job_id"], changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    {"code": "UNAUTHORIZED"}, {"code": "NETWORK_ERROR"}, {"code": "SOURCE_NOT_FOUND"},
    {"code": "COMPILE_INCOMPLETE", "message": "Unknown runtime failure"},
])
async def test_contract_upgrade_does_not_grant_unknown_or_environment_failures(tmp_path, error):
    _, repo, _, failed, _ = await failed_second_round(tmp_path)
    different = {**failed, "native_failure": {}, "error": error, "ov_pipeline": {"errors": []}}
    with pytest.raises(ValueError, match="仅明确"):
        prepare_recovery(repo.jobs_root / failed["job_id"], different)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_expired_round_budget_cannot_be_renewed_by_first_or_later_recovery(tmp_path, legacy):
    owner, repo, remote, failed, task = await failed_second_round(tmp_path)
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    failed = repo.update_job(failed["job_id"], round_started_at=expired,
        **({"native_resource_recovery": {"round": 2, "protocol": "te-native-resource-recovery.v1"}} if legacy else {}))
    with pytest.raises(ValueError, match="预算已到期"):
        repo.resume_native_failure(failed["job_id"], task)
    with pytest.raises(ValueError, match="预算已到期"):
        await owner._resume_native_resource_failure(failed["job_id"])
    assert len(remote.compile_calls) == 2 and repo.get_job(failed["job_id"])["round_started_at"] == expired


@pytest.mark.asyncio
async def test_recovery_freezes_guidance_before_first_post_across_software_upgrade(tmp_path):
    owner, repo, remote, failed, _ = await failed_second_round(tmp_path)
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    approved = request_guidance(AUXILIARY_FILES)
    key = recovered["submission_idempotency_key"]
    root = repo.jobs_root / failed["job_id"]
    newer = approved.replace(RESOURCE_POLICY, "te-declared-skill-resources.v2")
    assert pending_guidance(root, recovered, newer) == approved
    with patch("team_miner.skill_miner.native_compiler.request_guidance", return_value=newer):
        submitted = await owner._submit_ov_skill_round(recovered)
    assert submitted["submission_idempotency_key"] == key and len(remote.compile_calls) == 3
    assert submitted["compile_request"]["instruction"].endswith(approved)
    assert "te-declared-skill-resources.v2" not in submitted["compile_request"]["instruction"]


@pytest.mark.asyncio
async def test_pending_guidance_rejects_modified_journal_and_frozen_artifacts(tmp_path):
    owner, repo, _, failed, _ = await failed_second_round(tmp_path)
    recovered = await owner._resume_native_resource_failure(failed["job_id"])
    root = repo.jobs_root / failed["job_id"]
    path = next((root / "round_inputs/round-2/native-recoveries").glob("*.json"))
    receipt = json.loads(path.read_text())
    path.write_text(json.dumps({**receipt, "guidance": receipt["guidance"] + "changed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="指导已改变"):
        pending_guidance(root, recovered, "current")
    path.write_text(json.dumps(receipt), encoding="utf-8")
    skill = repo.workspace(recovered) / "compiled_skill/policy-review/SKILL.md"
    skill.write_text(skill.read_text() + "changed", encoding="utf-8")
    with pytest.raises(ValueError, match="冻结的请求、来源或产物发生变化"):
        pending_guidance(root, recovered, "current")


@pytest.mark.asyncio
async def test_output_protocol_failure_cannot_use_unrelated_resource_guidance_recovery(tmp_path):
    owner, repo, remote, failed, task = await failed_second_round(tmp_path, protocol_failure=True)
    assert failed["native_failure"]["kind"] == "output_protocol"
    assert repo.public_job(failed)["can_resume"] is False
    with pytest.raises(ValueError, match="输出协议失败须独立修复"):
        repo.resume_native_failure(failed["job_id"], task)
    with pytest.raises(ValueError, match="输出协议失败须独立修复"):
        await owner._resume_native_resource_failure(failed["job_id"])
    assert len(remote.compile_calls) == 2 and not failed.get("native_contract_recoveries")


@pytest.mark.asyncio
async def test_wrapping_same_legacy_resource_body_is_not_a_guidance_upgrade(tmp_path):
    owner, repo, remote, failed, task = await failed_second_round(tmp_path,
        old_guidance=resource_policy(AUXILIARY_FILES))
    assert repo.public_job(failed)["can_resume"] is False
    with pytest.raises(ValueError, match="同一契约指导版本"):
        repo.resume_native_failure(failed["job_id"], task)
    with pytest.raises(ValueError, match="同一契约指导版本"):
        await owner._resume_native_resource_failure(failed["job_id"])
    assert len(remote.compile_calls) == 2
