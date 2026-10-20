"""Exercise native submission, incremental input and artifact acceptance through the public host path."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    COMPONENT_PROTOCOL,
    METHOD_SCHEMA,
    PROTOCOL,
    SKILL_BODY,
    compiler_integrity_view,
    frozen_assets,
)
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT, OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_document
from tests.miner.skill_miner.support.ov_round_bridge import SOURCE, FakeCompileClient, FakeOV, Owner


class NativeClient(FakeCompileClient):
    async def install_skill(self, **kwargs):
        result = await super().install_skill(**kwargs)
        if result.get("ok"):
            if not hasattr(self.remote, "compiler_assets"):
                self.remote.compiler_assets = {}
            self.remote.compiler_assets[kwargs["skill_name"]] = kwargs.get("auxiliary_files", {})
        return result

    async def get_skill(self, **kwargs):
        result = await super().get_skill(**kwargs)
        if result.get("ok"):
            files = {"SKILL.md": result["result"]["content"],
                     **getattr(self.remote, "compiler_assets", {}).get(kwargs["skill_name"], {})}
            result["result"]["files"] = [{"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                                         for path, text in files.items()]
        return result


class NativeOwner(Owner):
    def _round_client(self, job):
        return NativeClient(self.remote)


def test_compiler_manifest_binds_exact_public_digest_privacy_template_only():
    import re

    from team_miner.skill_miner.ov_round_orchestration import _compiler_package
    from team_miner.skill_miner.platform_client import _skill_package_files, _skill_package_matches

    package, body, _ = _compiler_package(SKILL_BODY)
    files = _skill_package_files(package, body, AUXILIARY_FILES)
    masked = re.sub(r"(?m)(^compiler_assets_sha256: )[a-f0-9]{64}$",
                    r"\g<1>{{ov_privacy:skill:" + package + ":compiler_assets_sha256}}", body)
    detail = {"content": body, "files": [
        {"path": path, "sha256": hashlib.sha256(masked.encode() if path == "SKILL.md" else data).hexdigest()}
        for path, data in files.items()
    ]}
    original = json.loads(json.dumps(detail))
    assert not _skill_package_matches(detail, body, files)
    assert _skill_package_matches(compiler_integrity_view(detail, body, package), body, files)
    assert detail == original
    detail["files"][0]["sha256"] = "0" * 64
    assert not _skill_package_matches(compiler_integrity_view(detail, body, package), body, files)
    detail["content"] = body + "\n被修改的正文"
    assert compiler_integrity_view(detail, body, package) == detail


def new_owner(tmp_path, *, rounds=2, mode="single", compiler=SKILL_BODY):
    repo = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
    remote = FakeOV()
    remote.content[SOURCE + "/policy.md"] = "依据原始地区规则核对申请，资料不足时询问并保留结论，禁止猜测资格。"
    for i in range(4):
        remote.content[SOURCE + f"/other-{i}.md"] = f"第{i}份独立背景，不影响地区规则。"
    owner = NativeOwner(repo, remote)
    owner.compiler_body = compiler
    job = repo.create_job(name="原生挖掘验收", account="default", source_uri=SOURCE,
        source_snapshot_uri=RUNS_ROOT + "/pending/source", output_uri=RUNS_ROOT + "/pending/output",
        document_count=5, max_rounds=rounds, skip_empty_checkpoints=True, compiler_body=compiler,
        mining_mode=mode, mining_direction="完整申请核对方法" if mode == "single" else "")
    return owner, repo, remote, job


def complete(remote, job, *, question=False, names=("policy-review",), foreign=False):
    source = job["original_source_snapshot"] + "/original/policy.md"
    refs = ["viking://resources/foreign/file.md"] if foreign else [source]
    for name in names:
        record = {"schema_version": METHOD_SCHEMA, "name": name, "task": "完整核对申请",
                  "trigger": "收到地区申请", "inputs": ["申请与原始地区规则"], "outputs": ["有依据的核对结果"],
                  "boundary": "仅核对申请，不替代真实资格决定", "rationale": "完整且独立的申请核对任务",
                  "source_refs": refs, "questions": [{"qid": "region", "question": "地区适用范围是什么？",
                      "reason": "原文尚未给出地区", "source_refs": refs, "required": True}] if question else []}
        document = build_document(name, [{"query": f"第{i}个地区申请的核对", "name": f"地区核对情境{i}",
            "requirements": [f"核对条件{n}的来源与适用范围" for n in range(12)],
            "trajectory_requirements": ["先阅读原始地区规则再核对申请"], "source_session_ids": refs}
            for i in range(12)])
        document["name"] = "地区申请核对任务集合"
        document["description"] = "验证原始地区规则的完整应用"
        base = job["output_uri"] + f"/compiled_skill/{name}/"
        remote.content.update({
            base + "METHOD.json": json.dumps(record, ensure_ascii=False),
            base + "SKILL.md": (f"---\nname: {name}\ndescription: 依据来源完整核对地区申请。\n"
                                "---\n# 地区申请核对\n\n按原文核对。"),
            base + "EVALUATION.md": "# 地区核对评测\n\n保留条件并核实来源。",
            base + "benchmark.json": json.dumps(document, ensure_ascii=False),
        })
        if job.get("native_component_protocol"):
            prefix = f"analysis/components/{name}"
            reference = f"compiled_skill/{name}/references/conditions.md"
            descriptors = []
            for case in document["cases"]:
                path = f"{prefix}/cases/{case['case_id']}.json"
                descriptors.append({"path": path, "case_id": case["case_id"], "source_refs": refs})
                remote.content[job["output_uri"] + "/" + path] = json.dumps(case, ensure_ascii=False)
            manifest = {"schema_version": job["native_component_protocol"], "name": name,
                "dataset": {key: value for key, value in document.items() if key != "cases"},
                "references": [{"path": reference, "source_refs": refs}], "cases": descriptors,
                "coverage": [{"source_ref": ref, "reference_paths": [reference],
                    "case_ids": [case["case_id"] for case in document["cases"]],
                    "disposition": "included", "reason": "完整核对来源的地区适用条件"} for ref in refs]}
            remote.content[job["output_uri"] + "/" + reference] = "# 地区适用条件\n\n按原始地区规则核对申请。"
            remote.content[job["output_uri"] + "/" + prefix + "/manifest.json"] = json.dumps(
                manifest, ensure_ascii=False,
            )
    remote.tasks[job["ov_task_id"]] = {"status": "completed", "stage": "compile: completed"}


@pytest.mark.asyncio
async def test_two_rounds_use_two_native_tasks_same_target_and_only_implicated_original(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(job)
    assert first["execution_protocol"] == PROTOCOL and "staged_protocol" not in first
    assert len(remote.compile_calls) == 1 and first["compile_request"]["to"] == first["stable_output_uri"]
    manifest = repo.jobs_root / first["job_id"] / "round_inputs/round-1/manifest.json"
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(first["job_id"])
    assert waiting["status"] == "waiting_human" and waiting["candidate_count"] == 1
    skill = repo.workspace(waiting) / "compiled_skill/policy-review/SKILL.md"
    first_bytes = skill.read_bytes()
    second = repo.prepare_next_round(waiting["job_id"], waiting["pending_checkpoint"]["id"],
        {"region": "仅适用于甲地区。"}, [], {}, "", "request-2", submitted_by="admin")
    assert second["output_uri"] == first["output_uri"] and skill.read_bytes() == first_bytes
    second = await owner._submit_ov_skill_round(second)
    assert len(remote.compile_calls) == 2 and len(second["native_compile_history"]) == 2
    assert second["compile_request"]["from"] != first["compile_request"]["from"]
    entries = json.loads((repo.jobs_root / job["job_id"] / "round_inputs/round-2/manifest.json").read_text())
    assert [row["relative_path"] for row in entries if row["kind"] == "source"] == ["original/policy.md"]
    assert len(entries) == 4
    assert [row["relative_path"] for row in entries if row["kind"] == "context"] == [
        "revision/method-catalog.json", "revision/component-catalog.json",
    ]
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_sha
    complete(remote, second)
    await owner._refresh_ov_skill_job(second)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and len(done["rounds_history"]) == 2
    assert done["skipped_checkpoints"][-1]["human_approved"] is False
    assert (repo.workspace(done) / "compiled_skill/policy-review/BENCHMARK.md").is_file()
    archive = repo.project_root / done["rounds_history"][0]["path"] / "output/compiled_skill/policy-review/SKILL.md"
    assert archive.read_bytes() == first_bytes


@pytest.mark.asyncio
async def test_empty_review_finishes_without_spending_another_compile(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, rounds=5)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job)
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and done["current_round"] == 1
    assert len(remote.compile_calls) == 1 and len(done["rounds_history"]) == 1
    assert done["checkpoint_history"] == [] and done["supplements"] == []
    assert done["skipped_checkpoints"][0]["new_evidence"] is False


@pytest.mark.asyncio
async def test_disabled_human_checkpoint_preserves_questions_without_an_empty_next_round(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, rounds=3)
    job = repo.update_job(job["job_id"], human_checkpoints_effective=False)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job, question=True)
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and done["current_round"] == 1
    assert len(remote.compile_calls) == 1
    assert repo._read_review(done)["questions"][0]["qid"] == "region"
    assert done["checkpoint_history"] == [] and done["supplements"] == []


@pytest.mark.asyncio
async def test_later_question_can_reference_an_original_frozen_again_in_the_second_round(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, rounds=3)
    for number in (1, 2):
        job = await owner._submit_ov_skill_round(job)
        complete(remote, job, question=True)
        if number == 2:
            path = job["output_uri"] + "/compiled_skill/policy-review/METHOD.json"
            record = json.loads(remote.content[path])
            record["questions"][0]["source_refs"] = [job["source_snapshot"] + "/original/policy.md"]
            remote.content[path] = json.dumps(record, ensure_ascii=False)
        await owner._refresh_ov_skill_job(job)
        waiting = repo.get_job(job["job_id"])
        assert waiting["status"] == "waiting_human"
        job = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
            {"region": f"第{number}次确认甲地区。"}, [], {}, "", f"request-{number}", submitted_by="admin")
    third = await owner._submit_ov_skill_round(job)
    manifest = repo.jobs_root / job["job_id"] / "round_inputs/round-3/manifest.json"
    entries = json.loads(manifest.read_text())
    assert [row["relative_path"] for row in entries if row["kind"] == "source"] == ["original/policy.md"]
    assert third["compile_request"]["to"] == third["stable_output_uri"]
    assert len({request[1]["to"] for request in remote.compile_calls}) == 1
    assert len(remote.compile_calls) == 3


@pytest.mark.asyncio
async def test_lost_acceptance_reconciles_same_native_task_without_second_post(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(job)
    assert uncertain["status"] == "preparing" and not uncertain["ov_task_id"]
    frozen = uncertain["compile_request"]
    resumed = await owner._submit_ov_skill_round(uncertain)
    assert resumed["status"] == "queued" and resumed["compile_request"] == frozen
    assert len(remote.compile_calls) == 1 and len(resumed["native_compile_history"]) == 1
    complete(remote, resumed)
    await owner._refresh_ov_skill_job(resumed)
    assert repo.get_job(job["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_admission_wait_then_restart_past_deadline_does_not_start_compile(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    admissions = []

    async def wait_for_capacity(current):
        admissions.append(current["submission_idempotency_key"])
        return False

    owner._native_admit = wait_for_capacity
    waiting = await owner._submit_ov_skill_round(job)
    assert waiting["status"] == "preparing" and not remote.compile_calls
    original_request = waiting["compile_request"]
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    waiting = repo.update_job(job["job_id"], round_started_at=expired)
    failed = await owner._submit_ov_skill_round(waiting)
    assert failed["status"] == "failed" and "预算已到期" in failed["error"]
    assert failed["compile_request"] == original_request and failed["round_started_at"] == expired
    assert len(admissions) == 1 and remote.compile_calls == []
    assert remote.submission_lookups == [waiting["submission_idempotency_key"]]


@pytest.mark.asyncio
async def test_budget_expiring_during_admission_releases_only_unposted_fresh_slot(tmp_path):
    from tests.miner.skill_miner.support import admission as compile_admission

    owner, repo, remote, job = new_owner(tmp_path)
    original_admit = owner._native_admit

    async def admit_then_expire(current):
        admitted = await original_admit(current)
        current["round_started_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        repo.update_job(current["job_id"], round_started_at=current["round_started_at"])
        return admitted

    owner._native_admit = admit_then_expire
    failed = await owner._submit_ov_skill_round(job)
    assert failed["status"] == "failed" and "预算已到期" in failed["error"]
    assert remote.compile_calls == [] and compile_admission.reservations(repo.jobs_root) == []


@pytest.mark.asyncio
async def test_expired_lost_acceptance_is_reconciled_before_budget_or_admission_check(tmp_path):
    from tests.miner.skill_miner.support import admission as compile_admission

    owner, repo, remote, job = new_owner(tmp_path)
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(job)
    key = uncertain["submission_idempotency_key"]
    assert len(remote.compile_calls) == 1 and compile_admission.reservations(repo.jobs_root)[0]["key"] == key
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    uncertain = repo.update_job(job["job_id"], round_started_at=expired)

    async def no_admission_before_lookup(_):
        raise AssertionError("An already accepted task must be reconciled before admission")

    owner._native_admit = no_admission_before_lookup
    resumed = await owner._submit_ov_skill_round(uncertain)
    assert resumed["status"] == "queued" and resumed["round_started_at"] == expired
    assert len(remote.compile_calls) == 1 and resumed["compile_request"] == uncertain["compile_request"]
    reserved = compile_admission.reservations(repo.jobs_root)
    assert len(reserved) == 1 and reserved[0]["task_id"] == resumed["ov_task_id"]
    complete(remote, resumed)
    await owner._refresh_ov_skill_job(resumed)
    assert repo.get_job(job["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_expired_missing_acceptance_does_not_post_or_discard_uncertain_reservation(tmp_path):
    from starlette.exceptions import HTTPException

    from tests.miner.skill_miner.support import admission as compile_admission

    owner, repo, remote, job = new_owner(tmp_path)
    original_request = owner._knowledge_request
    post_attempts = []

    async def uncertain_post(account, method, path, **kwargs):
        if method == "POST" and path == "/api/v1/compile":
            post_attempts.append(kwargs["extra_headers"]["Idempotency-Key"])
            raise HTTPException(503, "Uncertain acceptance")
        return await original_request(account, method, path, **kwargs)

    owner._knowledge_request = uncertain_post
    uncertain = await owner._submit_ov_skill_round(job)
    assert uncertain["status"] == "preparing" and len(post_attempts) == 1
    reserved = compile_admission.reservations(repo.jobs_root)
    expired = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    uncertain = repo.update_job(job["job_id"], round_started_at=expired)
    failed = await owner._submit_ov_skill_round(uncertain)
    assert failed["status"] == "failed" and "预算已到期" in failed["error"]
    assert len(post_attempts) == 1 and remote.submission_lookups == [uncertain["submission_idempotency_key"]]
    assert compile_admission.reservations(repo.jobs_root) == reserved


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 422, 503])
async def test_rejected_native_submission_releases_only_proven_rejected_reservations(tmp_path, status):
    from starlette.exceptions import HTTPException

    from tests.miner.skill_miner.support import admission as compile_admission

    owner, repo, _, job = new_owner(tmp_path)
    original = owner._knowledge_request

    async def reject(account, method, path, **kwargs):
        if method == "POST" and path == "/api/v1/compile":
            raise HTTPException(status_code=status, detail="工程夹具模拟受理响应")
        return await original(account, method, path, **kwargs)

    owner._knowledge_request = reject
    result = await owner._submit_ov_skill_round(job)
    reservations = compile_admission.reservations(repo.jobs_root)
    if status == 503:
        assert result["status"] == "preparing" and len(reservations) == 1
        assert reservations[0]["key"] == result["submission_idempotency_key"]
    else:
        assert result["status"] == "failed" and reservations == []


@pytest.mark.asyncio
async def test_foreign_citation_rejects_entire_projection_without_replacing_workspace(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    original = repo.write_output(job["job_id"], "analysis/old.md", "原有产物不可丢弃")
    complete(remote, job, foreign=True)
    with pytest.raises(ValueError, match="已冻结|未知来源"):
        await owner._materialize_native_round(job)
    assert original.read_text() == "原有产物不可丢弃"
    assert not (repo.workspace(job) / "compiled_skill/policy-review").exists()


@pytest.mark.asyncio
async def test_free_discovery_is_not_limited_to_eight_candidates(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, mode="auto")
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job, names=tuple(f"method-{i}" for i in range(10)))
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and done["valid_candidate_count"] == 10
    assert len(remote.compile_calls) == 1


@pytest.mark.asyncio
async def test_invalid_artifacts_stop_without_recompile_and_keep_accepted_request(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    first_task, first_request = job["ov_task_id"], job["compile_request"]
    complete(remote, job, foreign=True)
    await owner._refresh_ov_skill_job(job)
    failed = repo.get_job(job["job_id"])
    assert failed["status"] == "failed" and not failed.get("native_repairs")
    assert failed["ov_task_id"] == first_task and failed["compile_request"] == first_request
    assert failed["submission_idempotency_key"] == job["submission_idempotency_key"]
    assert failed["native_compile_history"] == job["native_compile_history"]
    assert failed["native_failure"]["kind"] == "artifact_validation"
    assert len(remote.compile_calls) == 1 and not failed["native_repair_instruction"]
    archive = repo.project_root / failed["native_failure"]["archive"]
    assert json.loads(archive.read_text())["kind"] == "artifact_validation"
    requests = list((repo.jobs_root / job["job_id"] / "round_inputs/round-1/native-requests").glob("*.json"))
    assert len(requests) == 1 and json.loads(requests[0].read_text()) == first_request


@pytest.mark.asyncio
async def test_truncated_native_task_stops_after_one_compile_and_preserves_old_repairs(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    legacy = [{"round": 1, "attempt": 1, "task_id": "historical-native", "request": {"legacy": True}}]
    job = repo.update_job(job["job_id"], native_repairs=legacy)
    remote.tasks[job["ov_task_id"]] = {"status": "failed", "error": {"code": "COMPILE_INCOMPLETE"},
        "meta": {"pipeline": {"errors": [
            "Agent output was truncated; use smaller writes or content_ref and complete tool calls"]}}}
    await owner._refresh_ov_skill_job(job)
    failed = repo.get_job(job["job_id"])
    assert failed["status"] == "failed" and failed["native_repairs"] == legacy
    assert len(remote.compile_calls) == len(failed["native_compile_history"]) == 1
    assert failed["native_failure"]["kind"] == "output_truncated"
    assert failed["ov_task_id"] == job["ov_task_id"] and failed["compile_request"] == job["compile_request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen_request", [False, True])
async def test_unapproved_same_round_key_cannot_bypass_one_compile_limit(tmp_path, frozen_request):
    owner, repo, remote, job = new_owner(tmp_path)
    accepted = await owner._submit_ov_skill_round(job)
    root = repo.jobs_root / job["job_id"]
    requests = root / "round_inputs/round-1/native-requests"
    original = {p.name: p.read_bytes() for p in requests.glob("*.json")}
    # Simulate a persisted job from the removed automatic whole-round repair path.
    stale = repo.update_job(job["job_id"], status="preparing", ov_task_id="",
        submission_idempotency_key=accepted["submission_idempotency_key"] + "-repair-1",
        compile_request=accepted["compile_request"] if frozen_request else {})
    failed = await owner._submit_ov_skill_round(stale)
    assert failed["status"] == "failed" and "本轮仅一次 Compile" in failed["error"]
    assert len(remote.compile_calls) == 1 and failed["native_compile_history"] == accepted["native_compile_history"]
    assert {p.name: p.read_bytes() for p in requests.glob("*.json")} == original


@pytest.mark.asyncio
async def test_recorded_acceptance_with_remote_404_is_never_posted_again(tmp_path):
    from tests.miner.skill_miner.support import admission as compile_admission

    owner, repo, remote, job = new_owner(tmp_path)
    accepted = await owner._submit_ov_skill_round(job)
    reservations = compile_admission.reservations(repo.jobs_root)
    # The local accepted receipt is durable even if the remote idempotency lookup is lost.
    stale = repo.update_job(job["job_id"], status="preparing", ov_task_id="")
    remote.lookup_supported = False
    failed = await owner._submit_ov_skill_round(stale)
    assert failed["status"] == "failed" and "已有此标识" in failed["error"]
    assert failed["compile_request"] == accepted["compile_request"]
    assert failed["native_compile_history"] == accepted["native_compile_history"]
    assert len(remote.compile_calls) == 1
    assert compile_admission.reservations(repo.jobs_root) == reservations


@pytest.mark.asyncio
async def test_recorded_acceptance_with_remote_200_only_reconciles_same_task(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    remote.lookup_supported = True
    accepted = await owner._submit_ov_skill_round(job)
    stale = repo.update_job(job["job_id"], status="preparing", ov_task_id="")
    reconciled = await owner._submit_ov_skill_round(stale)
    assert reconciled["ov_task_id"] == accepted["ov_task_id"]
    assert reconciled["compile_request"] == accepted["compile_request"]
    assert reconciled["native_compile_history"] == accepted["native_compile_history"]
    assert len(remote.compile_calls) == 1


@pytest.mark.asyncio
async def test_incremental_review_carries_only_changed_artifact_as_non_evidence_context(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job, question=True)
    await owner._refresh_ov_skill_job(job)
    waiting = repo.get_job(job["job_id"])
    skill = repo.workspace(waiting) / "compiled_skill/policy-review/SKILL.md"
    skill.write_text(skill.read_text() + "\n人工要求：核对前先确认地区。\n")
    next_round = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {"region": "甲地区"}, [], {}, "", "edit-request", submitted_by="admin")
    submitted = await owner._submit_ov_skill_round(next_round)
    entries = json.loads((repo.jobs_root / job["job_id"] / "round_inputs/round-2/manifest.json").read_text())
    context = [row for row in entries if row["kind"] == "context"]
    assert [row["relative_path"] for row in context] == [
        "revision/method-catalog.json", "revision/component-catalog.json",
        "revision/compiled_skill/policy-review/SKILL.md"]
    assert all(submitted["source_snapshot"] + "/" + row["relative_path"]
               not in submitted["native_allowed_source_uris"] for row in context)


@pytest.mark.asyncio
async def test_incremental_method_catalog_preserves_identity_without_bodies_or_new_evidence(tmp_path):
    from pathlib import Path

    owner, repo, remote, job = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    next_round = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "catalog-review", submitted_by="admin")
    submitted = await owner._submit_ov_skill_round(next_round)
    root = repo.jobs_root / job["job_id"]
    manifest = json.loads((root / "round_inputs/round-2/manifest.json").read_text())
    entry = next(row for row in manifest if row["relative_path"] == "revision/method-catalog.json")
    path = Path(entry["local_path"])
    raw = path.read_bytes()
    catalog = json.loads(raw)
    method = root / "rounds/round-1/reviewed_output/compiled_skill/policy-review/METHOD.json"
    previous = json.loads(method.read_text())
    assert catalog["round"] == 1 and catalog["is_evidence"] is False and catalog["requires_source_review"] is True
    assert len(catalog["methods"]) == 1
    identity = catalog["methods"][0]
    assert identity["name"] == "policy-review" and identity["source_refs"] == previous["source_refs"]
    assert identity["method_sha256"] == hashlib.sha256(method.read_bytes()).hexdigest()
    assert set(identity) == set(previous) - {"schema_version", "questions"} | {"method_path", "method_sha256"}
    assert entry["kind"] == "context" and entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert submitted["source_snapshot"] + "/" + entry["relative_path"] not in submitted["native_allowed_source_uris"]
    assert "不是人工新增要求或 Evidence" in submitted["compile_request"]["instruction"]
    assert "不表示本轮必须重写目录中的所有方法" in submitted["compile_request"]["instruction"]
    # Frozen round inputs are reused, rather than silently adding/rebuilding context for an old request.
    method.write_text(method.read_text() + " ", encoding="utf-8")
    assert await owner._native_inputs(submitted) == manifest and path.read_bytes() == raw
    path.write_bytes(raw + b"tampered")
    with pytest.raises(ValueError, match="完整性校验失败"):
        await owner._native_inputs(submitted)


@pytest.mark.asyncio
async def test_frozen_incremental_inputs_without_catalog_keep_instruction_and_replay_bytes(tmp_path):
    from pathlib import Path

    from team_miner.skill_miner.ov_round_orchestration import _cache_inputs, _cached_inputs

    owner, repo, remote, job = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "legacy-catalog-free-review", submitted_by="admin")
    root = repo.jobs_root / job["job_id"]
    source = _cached_inputs(root / "round_inputs/round-1")[0]
    cache = root / "round_inputs/round-2"
    # A historical frozen round has the human review and implicated original, without the new catalog.
    _cache_inputs(cache, [
        {"kind": "human", "relative_path": "supplements/review.md", "content": "历史复核仍保留未解决问题。"},
        {**{k: source[k] for k in ("kind", "relative_path", "origin_uri")},
         "content": Path(source["local_path"]).read_text()},
    ])
    before = {p.name: p.read_bytes() for p in cache.iterdir() if p.is_file()}
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(second)
    frozen = uncertain["compile_request"]
    assert frozen["instruction"] == first["compile_request"]["instruction"].replace("第1轮", "第2轮")
    assert "method-catalog.json" not in frozen["instruction"]
    requests = root / "round_inputs/round-2/native-requests"
    request_bytes = {p.name: p.read_bytes() for p in requests.iterdir()}
    resumed = await owner._submit_ov_skill_round(uncertain)
    assert resumed["compile_request"] == frozen and len(remote.compile_calls) == 2
    assert {p.name: p.read_bytes() for p in requests.iterdir()} == request_bytes
    assert {p.name: p.read_bytes() for p in cache.iterdir() if p.is_file()} == before


@pytest.mark.asyncio
async def test_explicit_method_replacement_preserves_old_files_as_lineage(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, mode="auto")
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job, names=("opening", "closing", "visit"))
    base = job["output_uri"] + "/compiled_skill/visit/METHOD.json"
    record = json.loads(remote.content[base])
    record["supersedes"] = ["opening", "closing"]
    remote.content[base] = json.dumps(record, ensure_ascii=False)
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and done["candidate_count"] == 1
    assert (repo.workspace(done) / "semantic_reports/lineage/opening/SKILL.md").is_file()
    assert job["output_uri"] + "/compiled_skill/opening/SKILL.md" in remote.content


@pytest.mark.asyncio
async def test_compiler_assets_are_frozen_and_tampering_prevents_any_native_submission(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    root = repo.jobs_root / job["job_id"]
    assets = frozen_assets(root, SKILL_BODY)
    assert assets and COMPONENT_PROTOCOL in assets["references/output-contract.md"]
    assert f"component_protocol: {COMPONENT_PROTOCOL}" in SKILL_BODY
    assert "extract.execution=agent、output=records、input_unit=file" in SKILL_BODY
    (root / "compiler-assets/references/output-contract.md").write_text("篡改附件")
    failed = await owner._submit_ov_skill_round(job)
    assert failed["status"] == "failed" and "附件已改变" in failed["error"]
    assert remote.compile_calls == []


@pytest.mark.asyncio
async def test_completed_with_unknown_failed_internal_jobs_cannot_be_accepted_or_recompiled(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job)
    remote.tasks[job["ov_task_id"]]["meta"] = {"pipeline": {"errors": ["Source Map failed on an assigned range"]}}
    await owner._refresh_ov_skill_job(job)
    result = repo.get_job(job["job_id"])
    assert result["status"] == "failed" and not result["materialized"]
    assert result["native_failure"]["kind"] == "unknown"
    assert len(remote.compile_calls) == 1 and not result.get("native_repairs")


@pytest.mark.asyncio
async def test_duplicate_case_queries_are_not_accepted_as_diverse_cases(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job)
    uri = job["output_uri"] + "/compiled_skill/policy-review/benchmark.json"
    document = json.loads(remote.content[uri])
    document["cases"][1]["query"] = document["cases"][0]["query"]
    remote.content[uri] = json.dumps(document, ensure_ascii=False)
    case = document["cases"][1]
    component = job["output_uri"] + f"/analysis/components/policy-review/cases/{case['case_id']}.json"
    remote.content[component] = json.dumps(case, ensure_ascii=False)
    with pytest.raises(ValueError, match="重复"):
        await owner._materialize_native_round(job)


@pytest.mark.asyncio
async def test_components_are_assembled_without_a_second_compile_or_remote_dataset(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    assert job["native_component_protocol"] == COMPONENT_PROTOCOL
    complete(remote, job)
    del remote.content[job["output_uri"] + "/compiled_skill/policy-review/benchmark.json"]
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and len(remote.compile_calls) == 1
    root = repo.workspace(done)
    assert len(json.loads((root / "compiled_skill/policy-review/benchmark.json").read_text())["cases"]) == 12
    assert (root / "semantic_reports/components/policy-review/assembly-audit.json").is_file()
    assert (root / "compiled_skill/policy-review/references/conditions.md").is_file()


@pytest.mark.asyncio
async def test_required_missing_component_fails_without_hidden_recompile(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    job = await owner._submit_ov_skill_round(job)
    complete(remote, job)
    manifest_uri = job["output_uri"] + "/analysis/components/policy-review/manifest.json"
    case_path = json.loads(remote.content[manifest_uri])["cases"][0]["path"]
    del remote.content[job["output_uri"] + "/" + case_path]
    with pytest.raises(ValueError, match="缺少文件"):
        await owner._materialize_native_round(job)
    assert len(remote.compile_calls) == 1
    assert manifest_uri in remote.content


@pytest.mark.asyncio
async def test_frozen_legacy_compiler_accepts_its_original_four_file_contract(tmp_path):
    legacy = SKILL_BODY.replace(f"component_protocol: {COMPONENT_PROTOCOL}\n", "")
    owner, repo, remote, job = new_owner(tmp_path, compiler=legacy)
    job = await owner._submit_ov_skill_round(job)
    assert job["native_component_protocol"] is None
    complete(remote, job)
    remote.content[job["output_uri"] + "/compiled_skill/policy-review/references/legacy.md"] = "# Legacy reference"
    await owner._refresh_ov_skill_job(job)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and len(remote.compile_calls) == 1
    assert not list((repo.workspace(done) / "semantic_reports").rglob("manifest.json"))
