"""Submission guidance uses the deployed scratch-reference contract without rewriting frozen jobs."""

import hashlib
import json
from unittest.mock import patch

import pytest

from team_miner.native_compiler import (
    AUXILIARY_FILES,
    FILE_SUBMISSION,
    OUTPUT_CONTRACT,
    SKILL_BODY,
    UNIT_REVIEW,
    frozen_assets,
)
from team_miner.tests.test_native_mining_rounds import new_owner


def test_complete_file_manifest_is_distinct_from_individual_content_references():
    # A raw-source child also receives the whole main Skill. File submission must
    # be scoped to review, including when OV shares previously read attachments.
    assert FILE_SUBMISSION not in SKILL_BODY and FILE_SUBMISSION not in OUTPUT_CONTRACT
    assert FILE_SUBMISSION in UNIT_REVIEW
    assert "仅此职责输出FileResponse" in UNIT_REVIEW
    assert "本附件即使被共享注入也只在上述职责生效" in UNIT_REVIEW
    example = FILE_SUBMISSION.split("最后一次 emit 仅提交 ", 1)[1].split("；", 1)[0]
    assert json.loads(example) == {"result_ref": "submission.json"}
    assert "path 逐字使用当前targets中相对Compile目标的完整路径" in FILE_SUBMISSION
    assert "content_ref 是相对本 Agent scratch 根" in FILE_SUBMISSION
    assert "顶层为 files 数组" in FILE_SUBMISSION
    assert "不重复内联 content，也不混用 patches" in FILE_SUBMISSION
    assert "inputs 填写实际支持该文件的非空、去重输入ID" in FILE_SUBMISSION
    assert "base_hash 原样使用本次提供的历史文件摘要" in FILE_SUBMISSION
    assert "不能对不同文件多次成功 emit" in FILE_SUBMISSION
    assert "每个方法恰好12个有实质区别的案例" in OUTPUT_CONTRACT


@pytest.mark.asyncio
async def test_future_submission_guidance_keeps_frozen_compiler_assets_and_accepted_request(tmp_path):
    owner, repo, remote, job = new_owner(tmp_path)
    root = repo.jobs_root / job["job_id"]
    frozen = (root / "compiler-skill.md").read_bytes()
    asset_bytes = {path.relative_to(root): path.read_bytes() for path in (root / "compiler-assets").rglob("*")
                   if path.is_file()}
    # Updating defaults is not permission to migrate a job's already frozen package.
    with patch("team_miner.native_compiler.SKILL_BODY", SKILL_BODY + "\n未来版本说明"), patch(
            "team_miner.native_compiler.AUXILIARY_FILES", {"references/future.md": "未来交付契约"}):
        submitted = await owner._submit_ov_skill_round(job)
        assert submitted["status"] == "queued"
        payload = json.loads(json.dumps(submitted["compile_request"]))
        assert frozen_assets(root, frozen.decode()) == AUXILIARY_FILES
        assert await owner._submit_ov_skill_round(submitted) == submitted
    assert len(remote.compile_calls) == 1
    assert repo.get_job(job["job_id"])["compile_request"] == payload
    assert (root / "compiler-skill.md").read_bytes() == frozen
    assert hashlib.sha256(frozen).hexdigest() == submitted["compiler_source_sha256"]
    assert all((root / path).read_bytes() == content for path, content in asset_bytes.items())
