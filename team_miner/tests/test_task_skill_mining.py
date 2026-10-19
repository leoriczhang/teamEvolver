"""Exercise completion-only mining through submission, remote observation and local output."""

from pathlib import Path

import pytest

from team_miner.knowledge_compiler import AUXILIARY_FILES, SKILL_BODY
from team_miner.knowledge_compiler_v1 import AUXILIARY_FILES as POINT_V1_FILES
from team_miner.knowledge_compiler_v1 import SKILL_BODY as POINT_V1_SKILL
from team_miner.knowledge_compiler_v2 import AUXILIARY_FILES as POINT_V2_FILES
from team_miner.knowledge_compiler_v2 import SKILL_BODY as POINT_V2_SKILL
from team_miner.knowledge_compiler_v3 import AUXILIARY_FILES as POINT_V3_FILES
from team_miner.knowledge_compiler_v3 import SKILL_BODY as POINT_V3_SKILL
from team_miner.knowledge_compiler_v4 import AUXILIARY_FILES as POINT_V4_FILES
from team_miner.knowledge_compiler_v4 import SKILL_BODY as POINT_V4_SKILL
from team_miner.knowledge_compiler_v5 import AUXILIARY_FILES as POINT_V5_FILES
from team_miner.knowledge_compiler_v5 import SKILL_BODY as POINT_V5_SKILL
from team_miner.knowledge_compiler_v6 import AUXILIARY_FILES as POINT_V6_FILES
from team_miner.knowledge_compiler_v6 import SKILL_BODY as POINT_V6_SKILL
from team_miner.knowledge_compiler_v7 import AUXILIARY_FILES as POINT_V7_FILES
from team_miner.knowledge_compiler_v7 import SKILL_BODY as POINT_V7_SKILL
from team_miner.mining_completion import POLICY as COMPLETION_POLICY
from team_miner.mining_completion import completion_only
from team_miner.native_compiler import AUXILIARY_FILES as OLD_FILES
from team_miner.native_compiler import SKILL_BODY as OLD_SKILL
from team_miner.native_compiler import freeze_assets, frozen_assets
from team_miner.settings import compiler_prompt, settings_payload
from team_miner.skill_router_compiler import AUXILIARY_FILES as ROUTER_FILES
from team_miner.skill_router_compiler import SKILL_BODY as ROUTER_SKILL
from team_miner.skill_router_compiler_v3 import AUXILIARY_FILES as ROUTER_V3_FILES
from team_miner.skill_router_compiler_v3 import SKILL_BODY as ROUTER_V3_SKILL
from team_miner.task_compiler import AUXILIARY_FILES as TASK_FILES
from team_miner.task_compiler import SKILL_BODY as TASK_SKILL
from team_miner.tests.test_native_mining_rounds import new_owner


def test_new_default_retains_historical_skill_and_both_freeze_independently(tmp_path):
    for name, body, files in (("old", OLD_SKILL, OLD_FILES), ("task", TASK_SKILL, TASK_FILES),
                              ("point-v1", POINT_V1_SKILL, POINT_V1_FILES),
                              ("point-v2", POINT_V2_SKILL, POINT_V2_FILES),
                              ("point-v3", POINT_V3_SKILL, POINT_V3_FILES),
                              ("point-v4", POINT_V4_SKILL, POINT_V4_FILES),
                              ("point-v5", POINT_V5_SKILL, POINT_V5_FILES),
                              ("point-v6", POINT_V6_SKILL, POINT_V6_FILES),
                              ("point-v7", POINT_V7_SKILL, POINT_V7_FILES),
                              ("router", ROUTER_SKILL, ROUTER_FILES),
                              ("router-v3", ROUTER_V3_SKILL, ROUTER_V3_FILES),
                              ("new", SKILL_BODY, AUXILIARY_FILES)):
        folder = tmp_path / name
        folder.mkdir()
        freeze_assets(folder, body)
        assert frozen_assets(folder, body) == files
    assert compiler_prompt({}) == SKILL_BODY
    assert compiler_prompt({"mining": {"prompts": {"ov_compile": OLD_SKILL}}}) == OLD_SKILL
    assert settings_payload({})["prompts"][0]["previous_default_prompt"] == OLD_SKILL
    assert settings_payload({})["prompts"][0]["previous_knowledge_default_prompt"] == POINT_V1_SKILL
    assert settings_payload({})["prompts"][0]["previous_incremental_default_prompt"] == POINT_V2_SKILL
    assert settings_payload({})["prompts"][0]["previous_fragment_default_prompt"] == POINT_V3_SKILL
    assert settings_payload({})["prompts"][0]["previous_script_default_prompt"] == POINT_V4_SKILL
    assert settings_payload({})["prompts"][0]["previous_collection_default_prompt"] == POINT_V5_SKILL
    assert settings_payload({})["prompts"][0]["previous_anchor_default_prompt"] == POINT_V6_SKILL
    assert settings_payload({})["prompts"][0]["previous_directory_default_prompt"] == POINT_V7_SKILL
    for previous in (POINT_V1_SKILL, POINT_V2_SKILL, POINT_V3_SKILL, POINT_V4_SKILL, POINT_V5_SKILL,
                     POINT_V6_SKILL, POINT_V7_SKILL):
        saved = {"mining": {"prompts": {"ov_compile": previous}}}
        assert compiler_prompt(saved) == previous
        assert settings_payload(saved)["prompts"][0]["overridden"]


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [None, "这是未通过任何格式审核的正文"])
async def test_completed_without_benchmark_cases_or_valid_content_succeeds(tmp_path, returned):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto", rounds=3)
    job = await owner._submit_ov_skill_round(job)
    assert completion_only(job) and job["completion_policy"] == COMPLETION_POLICY
    assert job["native_component_protocol"] is None
    assert len(remote.compile_calls) == 1
    if returned is not None:
        remote.content[job["output_uri"] + "/compiled_skill/task-1/SKILL.md"] = returned
        remote.content[job["output_uri"] + "/compiled_skill/task-1/references/task-config.json"] = "invalid JSON"
    remote.tasks[job["ov_task_id"]] = {
        "status": "completed", "meta": {"pipeline": {"errors": ["原始产物存在未完成的子项"]}},
    }
    await owner._refresh_ov_skill_job(job)
    result = repo.get_job(job["job_id"])
    assert result["status"] == "succeeded" and result["materialized"]
    assert not result["content_validated"] and not result["pending_checkpoint"]
    assert result["candidate_count"] == (1 if returned is not None else 0)
    assert result["remote_warnings"]
    assert not list(repo.workspace(result).rglob("benchmark.json"))
    assert not list(repo.workspace(result).rglob("BENCHMARK.md"))
    assert len(remote.compile_calls) == 1
    public = repo.public_job(result, detail=True)
    assert public["status"] == "succeeded"
    if returned is not None:
        assert (repo.workspace(result) / "compiled_skill/task-1/SKILL.md").read_text() == returned
        repo.refresh_edited_quality(result["job_id"], "compiled_skill/task-1/SKILL.md")
        assert repo.get_job(job["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_local_sync_problem_warns_but_cannot_reject_remote_completion(tmp_path, monkeypatch):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    job = await owner._submit_ov_skill_round(job)
    remote.tasks[job["ov_task_id"]] = {"status": "completed"}

    async def unavailable(_job):
        raise ValueError("产物暂不可读")

    monkeypatch.setattr(owner, "_materialize_ov_skill_job", unavailable)
    await owner._refresh_ov_skill_job(job)
    result = repo.get_job(job["job_id"])
    assert result["status"] == "succeeded" and result["materialize_warnings"]
    assert result["output_uri"] == job["output_uri"]
    assert len(remote.compile_calls) == 1


@pytest.mark.asyncio
async def test_remote_execution_failure_is_not_disguised_as_success(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    job = await owner._submit_ov_skill_round(job)
    remote.tasks[job["ov_task_id"]] = {"status": "failed", "error": "Agent output was truncated;"}
    await owner._refresh_ov_skill_job(job)
    assert repo.get_job(job["job_id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_many_task_bundles_do_not_enter_old_case_or_candidate_gates(tmp_path, monkeypatch):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    job = await owner._submit_ov_skill_round(job)

    def obsolete_gate(*args, **kwargs):
        raise AssertionError("completion-only mining must not enter content acceptance")

    monkeypatch.setattr(repo, "_validate_candidate", obsolete_gate)
    for number in range(96):
        prefix = job["output_uri"] + f"/compiled_skill/task-{number}/"
        remote.content[prefix + "SKILL.md"] = f"任务 {number} 的原始返回正文"
        remote.content[prefix + "references/scenes.json"] = "[]"
    remote.content[job["output_uri"] + "/additional-notes.md"] = "保留契约外返回内容"
    remote.tasks[job["ov_task_id"]] = {"status": "completed"}
    await owner._refresh_ov_skill_job(job)
    result = repo.get_job(job["job_id"])
    assert result["status"] == "succeeded" and result["candidate_count"] == 96
    assert len(list(repo.workspace(result).glob("compiled_skill/*/references/scenes.json"))) == 96
    assert (repo.workspace(result) / "additional-notes.md").is_file()
    assert len(remote.compile_calls) == 1


@pytest.mark.asyncio
async def test_unexpected_output_layout_is_preserved_without_content_rejection(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto")
    job = await owner._submit_ov_skill_round(job)
    remote.content[job["output_uri"] + "/compiled_skill"] = "远端返回了文件而不是目录"
    remote.tasks[job["ov_task_id"]] = {"status": "completed"}
    await owner._refresh_ov_skill_job(job)
    result = repo.get_job(job["job_id"])
    assert result["status"] == "succeeded" and result["candidate_count"] == 0


@pytest.mark.asyncio
async def test_completed_round_can_be_revised_without_content_acceptance(tmp_path, monkeypatch):
    import hashlib
    import json

    owner, repo, remote, job = new_owner(tmp_path, compiler=SKILL_BODY, mode="auto", rounds=2)
    job = await owner._submit_ov_skill_round(job)
    relative = "compiled_skill/knowledge-15002/SKILL.md"
    remote.content[job["output_uri"] + "/" + relative] = "第一轮正文，无frontmatter或验证案例。"
    remote.content[job["output_uri"] + "/compiled_skill/knowledge-15002/references/config.json"] = "invalid JSON"
    remote.tasks[job["ov_task_id"]] = {"status": "completed"}
    await owner._refresh_ov_skill_job(job)
    first = repo.get_job(job["job_id"])
    checkpoint = repo.public_job(first)["optional_revision"]
    assert first["status"] == "succeeded" and first["pending_checkpoint"] is None
    assert checkpoint and len(remote.compile_calls) == 1
    archive = repo.jobs_root / job["job_id"] / "rounds/round-1/output" / relative
    original = archive.read_bytes()
    manifest = repo.jobs_root / job["job_id"] / "round_inputs/round-1/manifest.json"
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()

    for key, feedback in ((checkpoint["id"], ""), ("stale-round", "完善场景选择说明")):
        with pytest.raises(ValueError):
            repo.prepare_next_round(job["job_id"], key, {}, [], {}, feedback, "revision-2")
    assert repo.get_job(job["job_id"])["status"] == "succeeded"

    second = repo.prepare_next_round(job["job_id"], checkpoint["id"], {}, [], {},
                                    "完善场景选择说明", "revision-2", submitted_by="authorized-test")
    duplicate = repo.prepare_next_round(job["job_id"], checkpoint["id"], {}, [], {},
                                       "完善场景选择说明", "revision-2", submitted_by="authorized-test")
    assert duplicate["current_round"] == second["current_round"] == 2

    def obsolete_gate(*args, **kwargs):
        raise AssertionError("completion-only revision cannot invoke content validation")

    monkeypatch.setattr(repo, "_validate_candidate", obsolete_gate)
    second = await owner._submit_ov_skill_round(second)
    assert second["output_uri"] == first["output_uri"] and len(remote.compile_calls) == 2
    assert "完善场景选择说明" in second["compile_request"]["instruction"]
    inputs = json.loads((repo.jobs_root / job["job_id"] / "round_inputs/round-2/manifest.json").read_text())
    old_skill = next(row for row in inputs if row["relative_path"] == "revision/" + relative)
    assert old_skill["kind"] == "context" and Path(old_skill["local_path"]).read_bytes() == original
    assert any(row["kind"] == "source" for row in inputs)
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_sha
    assert archive.read_bytes() == original
    remote.content[second["output_uri"] + "/" + relative] = "第二轮修订，仍无验证案例。"
    remote.tasks[second["ov_task_id"]] = {"status": "completed"}
    await owner._refresh_ov_skill_job(second)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and len(done["rounds_history"]) == 2
    assert not done["content_validated"] and repo.public_job(done)["optional_revision"] is None
    assert (repo.workspace(done) / relative).read_text() == "第二轮修订，仍无验证案例。"
    assert archive.read_bytes() == original
    with pytest.raises(ValueError):
        repo.prepare_next_round(job["job_id"], checkpoint["id"], {}, [], {}, "第三轮", "revision-3")


def test_revision_context_enforces_transfer_capacity_before_accumulation(tmp_path, monkeypatch):
    from team_miner.mining_completion import revision_files
    from team_miner.ov_round_orchestration import INPUT_LIMITS

    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    first, second = reviewed / "a.md", reviewed / "b.md"
    first.write_text("未验收")
    monkeypatch.setitem(INPUT_LIMITS, "context", (1, 16, "上一轮修订上下文"))
    assert revision_files(tmp_path, reviewed)[0]["content"] == "未验收"
    second.write_text("extra")
    with pytest.raises(ValueError, match="容量"):
        revision_files(tmp_path, reviewed)
    second.unlink()
    first.write_text("a" * 17)
    with pytest.raises(ValueError, match="容量"):
        revision_files(tmp_path, reviewed)
