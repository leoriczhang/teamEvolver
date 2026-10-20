"""Completion-only inventory; content quality never changes remote mining success."""

from __future__ import annotations

import shutil
from pathlib import Path

POLICY = "te-compile-finished.v1"


def completion_only(job: dict) -> bool:
    return job.get("completion_policy") == POLICY


def revision_checkpoint(job: dict) -> dict | None:
    """Offer an optional revision after success, without making completion await approval."""
    if (not completion_only(job) or job.get("status") != "succeeded"
            or int(job["current_round"]) >= int(job["max_rounds"])):
        return None
    return {"id": f"{job['job_id']}-round-{job['current_round']}-revision",
            "round": job["current_round"], "checkpoint": "manual_revision",
            "title": "补充修订要求并继续挖掘", "intro": "本轮已成功；有明确修订要求时可继续下一轮。",
            "questions": []}


def revision_submission_job(job: dict, checkpoint_id: str, feedback: str) -> dict:
    """Reuse bounded submission validation; this temporary view is never persisted as a gate."""
    checkpoint = revision_checkpoint(job)
    if checkpoint is None or checkpoint_id != checkpoint["id"]:
        raise ValueError("该轮不能继续修订，或修订标识已过期")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError("继续挖掘需要明确的修订要求，不能自动空转")
    return {**job, "status": "waiting_human", "pending_checkpoint": checkpoint}


def revision_files(root: Path, reviewed: Path) -> list[dict]:
    """Carry returned bytes as context, without parsing or accepting any content contract."""
    from .ov_round_orchestration import INPUT_LIMITS, _append_input

    if reviewed.is_symlink() or root.resolve() not in reviewed.resolve().parents:
        raise ValueError("上一轮修订上下文目录越界或为符号链接")
    max_files, max_bytes, _ = INPUT_LIMITS["context"]
    files = []
    for path in sorted(reviewed.rglob("*")):
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError("上一轮修订上下文路径越界或包含符号链接")
        if path.is_file():
            if len(files) >= max_files:
                raise ValueError("上一轮修订上下文文件数超过传输容量")
            with path.open("rb") as stream:
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError("上一轮修订上下文文件超过传输容量")
            _append_input(files, {"kind": "context",
                                 "relative_path": "revision/" + path.relative_to(reviewed).as_posix(),
                                 "content": raw.decode("utf-8")})
    return files


def project_output(raw: Path, destination: Path) -> None:
    """Preserve returned files without parsing or requiring a content contract."""
    shutil.copytree(raw, destination)
    analysis = destination / "analysis"
    reports = destination / "semantic_reports"
    if analysis.is_dir():
        shutil.copytree(analysis, reports, dirs_exist_ok=True)
        shutil.rmtree(analysis)


def finalize_inventory(repository, job: dict) -> dict:
    """Discover files for the UI without interpreting them as accepted Skills."""
    root = repository.workspace(job)
    skills = root / "compiled_skill"
    rows, qualities = [], {}
    for folder in sorted(skills.iterdir()) if skills.is_dir() else []:
        if not folder.is_dir() or folder.is_symlink():
            continue
        entry = folder / "SKILL.md"
        has_skill = entry.is_file() and not entry.is_symlink()
        rows.append({"name": folder.name, "task": folder.name, "trigger": "",
                     "inputs": [], "outputs": [], "boundary": "", "rationale": "仅按返回目录列出，未执行内容验收",
                     "source_refs": []})
        qualities[folder.name] = {"has_skill": has_skill, "level": "ready", "label": "已生成",
                                "confidence": "未验收", "summary": "保留原始产物；不要求验证案例",
                                "warnings": [], "artifacts": [], "can_submit": False}
    plan = {"schema_version": "skill-miner.plan.v1", "skills": rows, "deferred": []}
    warnings = list(dict.fromkeys([*job.get("remote_warnings", []), *job.get("materialize_warnings", [])]))
    quality = {"level": "ready", "label": "挖掘已完成", "confidence": "未验收",
               "summary": "按远端完成状态记录成功，未执行内容验收；不要求 benchmark 或验证案例",
               "has_skill": any(row["has_skill"] for row in qualities.values()), "can_submit": False,
               "warnings": warnings, "artifacts": []}
    return repository.update_job(job["job_id"], materialized=True, artifact_quality=quality,
                                 capability_plan=plan, candidate_count=len(rows), candidate_quality=qualities,
                                 valid_candidate_count=0, content_validated=False)


def complete(repository, job: dict) -> dict:
    """Finish after remote completion, with no human/content acceptance gate."""
    from .ov_compile_mining import utc_now

    if str(job.get("ov_status") or "").lower() not in {"completed", "succeeded", "success"}:
        raise ValueError("远端挖掘尚未完成")
    history = list(job.get("rounds_history") or [])
    warnings = list(job.get("materialize_warnings") or [])
    try:
        metadata = repository._archive_round(job)
        history = [row for row in history if row.get("round") != metadata["round"]] + [metadata]
    except (OSError, ValueError) as exc:
        warnings.append(f"产物归档未完成：{exc}")
    quality = dict(job.get("artifact_quality") or {})
    quality.update(can_submit=False, warnings=list(dict.fromkeys([*quality.get("warnings", []), *warnings])))
    return repository.update_job(job["job_id"], status="succeeded", finished_at=utc_now(),
                                 round_completed_at=utc_now(), rounds_history=history, pending_checkpoint=None,
                                 artifact_quality=quality, materialize_warnings=warnings, error="",
                                 phase={"step1": "done", "step2": "done", "step3": "done"})
