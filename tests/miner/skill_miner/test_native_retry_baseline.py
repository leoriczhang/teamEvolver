"""Restore only byte-proven accepted raw artifacts before an unaccepted explicit retry."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_compiler import COMPONENT_PROTOCOL, NOTES_SCHEMA, SKILL_BODY
from team_miner.skill_miner.native_retry_baseline import baseline_plan, restore_baseline
from tests.miner.skill_miner.support.native_explicit_retry import authorization, hashes
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


async def rig(tmp_path, *, components=False, remote_dataset=True):
    # Keep the historical four-file recovery contract independently covered.
    compiler = SKILL_BODY if components else SKILL_BODY.replace(f"component_protocol: {COMPONENT_PROTOCOL}\n", "")
    owner, repo, remote, job = new_owner(tmp_path, compiler=compiler)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    if components and not remote_dataset:
        del remote.content[first["output_uri"] + "/compiled_skill/policy-review/benchmark.json"]
    remote.content[first["output_uri"] + "/analysis/notes/source-gap.json"] = json.dumps({
        "schema_version": NOTES_SCHEMA, "deferred": [{"reason": "原文未确定地区，保留未解决事项。",
            "source_refs": [first["original_source_snapshot"] + "/original/policy.md"]}], "questions": []},
        ensure_ascii=False)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "review-with-unresolved", submitted_by="admin")
    second = await owner._submit_ov_skill_round(second)
    task = {"task_id": second["ov_task_id"], "status": "completed", "error": None,
            "meta": {"pipeline": {"errors": ["map: schema: EmitRecords records.1.inputs Field required"]}}}
    remote.tasks[second["ov_task_id"]] = task
    partial = {}
    for uri in list(remote.content):
        if uri.startswith(second["output_uri"] + "/"):
            remote.content[uri] = "未验收的部分结果：" + remote.content[uri]
            partial[uri[len(second["output_uri"]) + 1:]] = remote.content[uri].encode("utf-8")
    await owner._refresh_ov_skill_job(second)
    failed = repo.get_job(job["job_id"])
    recovered = await owner._resume_native_resource_failure(job["job_id"], authorization(failed))
    root = repo.jobs_root / job["job_id"]
    folder = root / "round_inputs/round-2/native-explicit-retry" / (
        recovered["native_explicit_retry"]["authorization"]["request_id"] + "-baseline")
    writes = []
    original = owner._knowledge_request

    async def transport(account, method, path, **kwargs):
        if method == "POST" and path == "/api/v1/content/write" and kwargs["json"]["mode"] == "replace":
            manifest = json.loads((folder / "manifest.json").read_text())
            assert manifest["partial_sha256"] == {name: digest(data) for name, data in partial.items()}
            assert all((folder / "partial" / name).read_bytes() == data for name, data in partial.items())
            assert kwargs["json"]["wait"] is True
            remote.content[kwargs["json"]["uri"]] = kwargs["json"]["content"]
            writes.append(kwargs["json"]["uri"])
            return {"ok": True}
        if method == "POST" and path == "/api/v1/compile":
            assert (folder / "complete.json").is_file()
            desired = baseline_plan(root, recovered)["files"]
            assert {name: digest(remote.content[recovered["stable_output_uri"] + "/" + name])
                    for name in desired} == desired
        return await original(account, method, path, **kwargs)

    owner._knowledge_request = transport
    return owner, repo, remote, recovered, partial, writes, folder


@pytest.mark.asyncio
async def test_plan_proves_five_raw_files_and_refuses_modified_human_review(tmp_path):
    _, repo, _, job, _, _, _ = await rig(tmp_path)
    root = repo.jobs_root / job["job_id"]
    plan = baseline_plan(root, job)
    assert len(plan["files"]) == 5 and "analysis/notes/source-gap.json" in plan["files"]
    assert not any("BENCHMARK.md" in path or "semantic_reports" in path for path in plan["files"])
    skill = next((root / "rounds/round-1/reviewed_output/compiled_skill").glob("*/SKILL.md"))
    skill.write_text(skill.read_text() + "人工编辑尚未验收", encoding="utf-8")
    with pytest.raises(ValueError, match="人工复核版本已变化"):
        baseline_plan(root, job)
    # Dry-run eligibility must fail closed, rather than advertise a retry on an unknown baseline.
    failed = {**job, "status": "failed", "native_explicit_retry": None}
    assert repo.public_job(failed)["can_retry_native"] is False


@pytest.mark.asyncio
async def test_all_partial_bytes_are_archived_before_restore_and_first_post(tmp_path):
    owner, repo, remote, job, partial, writes, folder = await rig(tmp_path)
    root = repo.jobs_root / job["job_id"]
    before = hashes(root)
    submitted = await owner._submit_ov_skill_round(job)
    assert submitted["status"] == "queued" and len(remote.compile_calls) == 3
    assert len(writes) == 5 and len(set(writes)) == 5
    assert all((folder / "partial" / name).read_bytes() == data for name, data in partial.items())
    assert all(digest((root / path).read_bytes()) == sha for path, sha in before.items())
    assert json.loads((folder / "complete.json").read_text())["target_sha256"] == baseline_plan(root, job)["files"]


@pytest.mark.asyncio
async def test_extra_remote_file_blocks_without_writing_or_compile(tmp_path):
    owner, repo, remote, job, partial, writes, _ = await rig(tmp_path)
    remote.content[job["stable_output_uri"] + "/analysis/unexpected.md"] = "未知新增文件"
    rejected = await owner._submit_ov_skill_round(job)
    assert rejected["status"] == "failed" and "额外" in rejected["error"]
    assert not writes and len(remote.compile_calls) == 2
    assert all(remote.content[job["stable_output_uri"] + "/" + name].encode() == data
               for name, data in partial.items())
    assert repo.get_job(job["job_id"])["native_explicit_retry"] == job["native_explicit_retry"]


@pytest.mark.asyncio
async def test_interrupted_restore_continues_only_remaining_files_and_preserves_accepted_new_partial(tmp_path):
    owner, repo, remote, job, _, writes, _ = await rig(tmp_path)
    original = owner._knowledge_request
    interrupted = False

    async def disconnect_after_write(account, method, path, **kwargs):
        nonlocal interrupted
        result = await original(account, method, path, **kwargs)
        if (method == "POST" and path == "/api/v1/content/write" and kwargs["json"]["mode"] == "replace"
                and not interrupted):
            interrupted = True
            raise OSError("恢复写入完成但返回丢失")
        return result

    owner._knowledge_request = disconnect_after_write
    waiting = await owner._submit_ov_skill_round(job)
    assert waiting["status"] == "preparing" and len(writes) == 1 and len(remote.compile_calls) == 2
    owner._knowledge_request = original
    remote.lose_next_acceptance = True
    remote.lookup_supported = True
    uncertain = await owner._submit_ov_skill_round(waiting)
    assert uncertain["status"] == "preparing" and len(writes) == 5 and len(remote.compile_calls) == 3
    assert len(set(writes)) == 5
    current = {uri: "新 Native 的未验收结果" for uri in remote.content
               if uri.startswith(job["stable_output_uri"] + "/")}
    remote.content.update(current)
    accepted = await owner._submit_ov_skill_round(uncertain)
    assert accepted["status"] == "queued" and len(writes) == 5 and len(remote.compile_calls) == 3
    assert all(remote.content[uri] == data for uri, data in current.items())
    assert repo.get_job(job["job_id"])["ov_task_id"] == accepted["ov_task_id"]


@pytest.mark.asyncio
async def test_interrupted_restore_refuses_third_version_without_overwriting_it(tmp_path):
    owner, _, remote, job, _, writes, _ = await rig(tmp_path)
    original = owner._knowledge_request

    async def disconnect(account, method, path, **kwargs):
        result = await original(account, method, path, **kwargs)
        if method == "POST" and path == "/api/v1/content/write" and kwargs["json"]["mode"] == "replace":
            raise OSError("中断")
        return result

    owner._knowledge_request = disconnect
    waiting = await owner._submit_ov_skill_round(job)
    assert len(writes) == 1
    desired = baseline_plan(owner.repository.jobs_root / job["job_id"], job)["files"]
    changed = next(job["stable_output_uri"] + "/" + name for name in desired
                   if job["stable_output_uri"] + "/" + name not in writes)
    remote.content[changed] = "第三种人工修改"
    owner._knowledge_request = original
    rejected = await owner._submit_ov_skill_round(waiting)
    assert rejected["status"] == "failed" and "第三种" in rejected["error"]
    assert len(writes) == 1 and len(remote.compile_calls) == 2 and remote.content[changed] == "第三种人工修改"


@pytest.mark.asyncio
async def test_complete_marker_only_verifies_and_never_rolls_back_again(tmp_path):
    owner, _, remote, job, _, writes, _ = await rig(tmp_path)
    assert await restore_baseline(owner, job) is None
    assert len(writes) == 5 and len(remote.compile_calls) == 2
    changed = writes[0]
    remote.content[changed] = "完成记录之后产生的新内容"
    with pytest.raises(ValueError, match="拒绝再次回滚"):
        await restore_baseline(owner, job)
    assert len(writes) == 5 and remote.content[changed] == "完成记录之后产生的新内容"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_dataset", [False, True])
async def test_component_baseline_restores_only_existing_raw_components(tmp_path, remote_dataset):
    owner, repo, remote, job, partial, writes, _ = await rig(
        tmp_path, components=True, remote_dataset=remote_dataset,
    )
    root = repo.jobs_root / job["job_id"]
    plan = baseline_plan(root, job)
    dataset = "compiled_skill/policy-review/benchmark.json"
    assert len(plan["files"]) == 18 + int(remote_dataset)
    assert (dataset in plan["files"]) is remote_dataset
    assert "compiled_skill/policy-review/references/conditions.md" in plan["files"]
    assert "analysis/components/policy-review/manifest.json" in plan["files"]
    assert len([name for name in plan["files"] if "/cases/" in name]) == 12
    assert not any(name.endswith(("assembly-audit.json", "BENCHMARK.md")) for name in plan["files"])
    original = hashes(root)
    submitted = await owner._submit_ov_skill_round(job)
    assert submitted["status"] == "queued" and len(remote.compile_calls) == 3
    assert len(writes) == len(partial) == len(plan["files"])
    assert (job["stable_output_uri"] + "/" + dataset in remote.content) is remote_dataset
    assert all(digest((root / path).read_bytes()) == value for path, value in original.items())


@pytest.mark.asyncio
async def test_component_baseline_rejects_unproven_raw_files_without_writes(tmp_path):
    owner, repo, remote, job, _, writes, _ = await rig(tmp_path, components=True, remote_dataset=False)
    root = repo.jobs_root / job["job_id"]
    raw = root / baseline_plan(root, job)["raw_dir"]
    (raw / "analysis/components/policy-review/orphan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="完整原始文件包"):
        baseline_plan(root, job)
    assert not writes and len(remote.compile_calls) == 2


@pytest.mark.asyncio
async def test_component_baseline_replays_assembly_not_only_archive_hashes(tmp_path):
    _, repo, remote, job, _, writes, _ = await rig(tmp_path, components=True, remote_dataset=False)
    root = repo.jobs_root / job["job_id"]
    archive = root / "rounds/round-1"
    relative = "semantic_reports/components/policy-review/assembly-audit.json"
    audit = json.loads((archive / "output" / relative).read_text())
    audit["inputs"][0]["sha256"] = "0" * 64
    raw = json.dumps(audit, ensure_ascii=False).encode("utf-8")
    for directory in ("output", "reviewed_output"):
        (archive / directory / relative).write_bytes(raw)
    meta = json.loads((archive / "meta.json").read_text())
    meta["output_sha256"][relative] = digest(raw)
    (archive / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="装配审计与原始输入不等价"):
        baseline_plan(root, job)
    assert not writes and len(remote.compile_calls) == 2


@pytest.mark.asyncio
async def test_component_baseline_rejects_non_equivalent_local_dataset(tmp_path):
    _, repo, remote, job, _, writes, _ = await rig(tmp_path, components=True, remote_dataset=False)
    root = repo.jobs_root / job["job_id"]
    archive = root / "rounds/round-1"
    relative = "compiled_skill/policy-review/benchmark.json"
    dataset = json.loads((archive / "output" / relative).read_text())
    dataset["name"] = "改变后的本地集合"
    raw = json.dumps(dataset, ensure_ascii=False).encode("utf-8")
    for directory in ("output", "reviewed_output"):
        (archive / directory / relative).write_bytes(raw)
    meta = json.loads((archive / "meta.json").read_text())
    meta["output_sha256"][relative] = digest(raw)
    (archive / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="装配结果与验收产物不等价"):
        baseline_plan(root, job)
    assert not writes and len(remote.compile_calls) == 2
