from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from team_miner.skill_miner import artifacts

_METADATA = {}


def authoritative_metadata(root, path):
    parts = Path(path).parts
    identity = parts[1] if len(parts) > 1 else "job-1"
    return _METADATA.get((str(root), identity))


def _artifact(root: Path, path: str, *, status: str = "waiting_human", content: bytes = b"# Original\n") -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    job_id = Path(path).parts[1]
    (root / "mining_jobs" / job_id / "job.json").write_text(
        json.dumps({"job_id": job_id, "engine": "ov_compile", "status": status}), encoding="utf-8"
    )
    _METADATA[(str(root), job_id)] = {"job_id": job_id, "engine": "ov_compile", "status": status}
    return target


WORKSPACE_SKILL = "mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md"


def test_artifact_preview_and_edit_preserve_utf8_and_response_contract(tmp_path):
    target = _artifact(tmp_path, WORKSPACE_SKILL, content="# 原始 Skill\n".encode())
    preview = artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    assert preview == {
        "ok": True,
        "path": WORKSPACE_SKILL,
        "name": "SKILL.md",
        "kind": "skill",
        "content": "# 原始 Skill\n",
        "size_bytes": len("# 原始 Skill\n".encode()),
        "truncated": False,
        "edited": False,
    }
    assert artifacts.artifact_job_id(WORKSPACE_SKILL) == "job-1"
    saved = artifacts.save_artifact(
        tmp_path,
        WORKSPACE_SKILL,
        "# 修订 Skill\n\n阈值是 12000。\n",
        job=authoritative_metadata(tmp_path, WORKSPACE_SKILL),
    )
    assert saved["edited"] is True
    assert target.read_text("utf-8") == saved["content"]
    assert not list(target.parent.glob(".artifact-edit-*"))


@pytest.mark.parametrize("status", ["succeeded", "waiting_human"])
@pytest.mark.parametrize("file", ["SKILL.md", "EVALUATION.md", "BENCHMARK.md", "benchmark.json"])
def test_completed_skill_and_test_dataset_can_be_edited(tmp_path, status, file):
    path = f"mining_jobs/job-1/workspace/compiled_skill/demo/{file}"
    _artifact(tmp_path, path, status=status)
    assert (
        artifacts.save_artifact(tmp_path, path, "Updated", job=authoritative_metadata(tmp_path, path))["content"]
        == "Updated"
    )


def test_current_evidence_reports_can_be_edited_but_review_protocol_is_readonly(tmp_path):
    path = "mining_jobs/job-1/workspace/semantic_reports/gaps.md"
    _artifact(tmp_path, path)
    assert (
        artifacts.save_artifact(tmp_path, path, "# Evidence gaps\n", job=authoritative_metadata(tmp_path, path))["kind"]
        == "semantic"
    )
    review = "mining_jobs/job-1/workspace/semantic_reports/review.json"
    target = _artifact(tmp_path, review, content=b'{"questions": []}')
    assert (
        artifacts.read_artifact(tmp_path, review, job=authoritative_metadata(tmp_path, review))["content"]
        == '{"questions": []}'
    )
    with pytest.raises(ValueError, match="可编辑"):
        artifacts.save_artifact(tmp_path, review, "{}", job=authoritative_metadata(tmp_path, review))
    assert target.read_bytes() == b'{"questions": []}'


@pytest.mark.parametrize("location", ["workspace", "rounds/round-1/output", "rounds/round-1/reviewed_output"])
@pytest.mark.parametrize(
    "file,content",
    [
        ("rules.md", "# 配套规则\n"),
        ("nested/configuration.json", '{"范围":"退货"}'),
        ("SKILL.md", "# 同名资源\n"),
        ("EVALUATION.md", "# 同名评测资源\n"),
        ("BENCHMARK.md", "# 同名测试资源\n"),
        ("benchmark.json", "{}"),
    ],
)
def test_reference_resources_can_be_previewed_without_mutating_the_package(tmp_path, location, file, content):
    path = f"mining_jobs/job-1/{location}/compiled_skill/demo/references/{file}"
    target = _artifact(tmp_path, path, content=content.encode("utf-8"))
    preview = artifacts.read_artifact(tmp_path, path, job=authoritative_metadata(tmp_path, path))
    assert preview["kind"] == "resource"
    assert preview["content"] == content
    with pytest.raises(ValueError, match="只读|可编辑"):
        artifacts.save_artifact(tmp_path, path, "Changed", job=authoritative_metadata(tmp_path, path))
    assert target.read_bytes() == content.encode("utf-8")


@pytest.mark.parametrize("archive", ["output", "reviewed_output"])
def test_round_archive_can_be_viewed_and_cannot_be_edited(tmp_path, archive):
    path = f"mining_jobs/job-1/rounds/round-1/{archive}/compiled_skill/demo/SKILL.md"
    target = _artifact(tmp_path, path, status="running")
    assert (
        artifacts.read_artifact(tmp_path, path, job=authoritative_metadata(tmp_path, path))["content"] == "# Original\n"
    )
    with pytest.raises(ValueError, match="只读"):
        artifacts.save_artifact(tmp_path, path, "Changed", job=authoritative_metadata(tmp_path, path))
    assert target.read_bytes() == b"# Original\n"


@pytest.mark.parametrize("status", ["preparing", "queued", "running", "stopping", "failed", "stopped"])
def test_active_or_unfinished_job_cannot_be_edited(tmp_path, status):
    target = _artifact(tmp_path, WORKSPACE_SKILL, status=status)
    with pytest.raises(ValueError, match="已完成"):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "Changed", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )
    assert target.read_bytes() == b"# Original\n"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md",
        "mining_jobs/../workspace/compiled_skill/demo/SKILL.md",
        "mining_jobs/job-1/workspace/compiled_skill/demo/../../secret.md",
        "mining_jobs/job-1/job.json",
        "mining_jobs/job-1/workspace/data/input/private.md",
        "mining_jobs/job-1/rounds/round-1/input/original-source/private.md",
        "mining_jobs/job-1/rounds/round-1/input.json",
        "mining_jobs/job-1/rounds/round-1/meta.json",
        "mining_jobs/job-1/checkpoints/history/round-1.json",
        "mining_jobs/job-1/rounds/round-6/output/compiled_skill/demo/SKILL.md",
        "compiled_skill/demo/SKILL.md",
        "reflection_rounds/run-1/round_1/compiled_skill/demo/SKILL.md",
        "mining_jobs/job-1/workspace/compiled_skill/demo/.hidden.md",
        "mining_jobs/job-1/workspace/compiled_skill/demo/asset.png",
    ],
)
def test_control_files_input_snapshots_and_unsafe_paths_are_not_artifacts(tmp_path, path):
    _artifact(tmp_path, WORKSPACE_SKILL)
    with pytest.raises(ValueError):
        artifacts.read_artifact(tmp_path, path, job=authoritative_metadata(tmp_path, path))
    with pytest.raises(ValueError):
        artifacts.save_artifact(tmp_path, path, "Changed", job=authoritative_metadata(tmp_path, path))


@pytest.mark.parametrize("linked", ["file", "directory", "jobs"])
def test_symlinks_cannot_expose_or_modify_other_files(tmp_path, linked):
    target = _artifact(tmp_path, WORKSPACE_SKILL)
    outside = tmp_path / "private"
    outside.mkdir()
    secret = outside / "SKILL.md"
    secret.write_text("private", encoding="utf-8")
    if linked == "file":
        target.unlink()
        target.symlink_to(secret)
    elif linked == "directory":
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(outside, target_is_directory=True)
    elif linked == "metadata":
        metadata = tmp_path / "mining_jobs" / "job-1" / "job.json"
        metadata.unlink()
        metadata.symlink_to(secret)
    else:
        jobs = tmp_path / "mining_jobs"
        moved = tmp_path / "moved-jobs"
        jobs.rename(moved)
        jobs.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    with pytest.raises(ValueError, match="符号链接"):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "Changed", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )
    assert secret.read_text("utf-8") == "private"


def test_artifact_and_metadata_require_valid_utf8_and_current_engine(tmp_path):
    target = _artifact(tmp_path, WORKSPACE_SKILL, content=b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    target.write_bytes(b"text\x00binary")
    with pytest.raises(ValueError, match="二进制"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    _METADATA[(str(tmp_path), "job-1")] = {"job_id": "job-1", "engine": "local"}
    with pytest.raises(ValueError, match="元数据"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    _METADATA[(str(tmp_path), "job-1")] = None
    with pytest.raises(ValueError, match="元数据"):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "Changed", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )


def test_read_and_write_limits_count_utf8_bytes(tmp_path, monkeypatch):
    target = _artifact(tmp_path, WORKSPACE_SKILL, content=b"12345")
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 4)
    with pytest.raises(ValueError, match="大小限制"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))
    target.write_bytes(b"okay")
    with pytest.raises(ValueError, match="2 MB"):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "三个字", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )
    assert (
        artifacts.save_artifact(tmp_path, WORKSPACE_SKILL, "一", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))[
            "size_bytes"
        ]
        == 3
    )


def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(tmp_path, monkeypatch):
    target = _artifact(tmp_path, WORKSPACE_SKILL)

    def fail_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(artifacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "Changed", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )
    assert target.read_bytes() == b"# Original\n"
    assert not list(target.parent.glob(".artifact-edit-*"))


def test_missing_files_are_not_created_and_special_files_are_rejected(tmp_path):
    target = _artifact(tmp_path, WORKSPACE_SKILL)
    target.unlink()
    with pytest.raises(FileNotFoundError):
        artifacts.save_artifact(
            tmp_path, WORKSPACE_SKILL, "Changed", job=authoritative_metadata(tmp_path, WORKSPACE_SKILL)
        )
    os.mkfifo(target)
    with pytest.raises(ValueError, match="普通文件"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))


def test_local_job_json_cannot_override_authoritative_metadata(tmp_path):
    _artifact(tmp_path, WORKSPACE_SKILL)
    local = tmp_path / "mining_jobs/job-1/job.json"
    local.write_bytes(b"not authoritative JSON")
    assert artifacts.read_artifact(tmp_path, WORKSPACE_SKILL, job=authoritative_metadata(tmp_path, WORKSPACE_SKILL))[
        "content"
    ]
    with pytest.raises(ValueError, match="元数据"):
        artifacts.read_artifact(tmp_path, WORKSPACE_SKILL)
