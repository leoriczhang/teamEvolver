#!/usr/bin/env python3
"""
文档→Skill 挖掘流水线 · 全栈可视化控制台（后端）

纯 Python 标准库实现（http.server + threading + queue + SSE），零第三方依赖。

职责：
  1. 托管前端静态页（苹果风简约界面）
  2. 暴露流程的可配置项（GET /api/config）
  3. 一键运行整条流水线（POST /api/run），通过 SSE（GET /api/events）实时推送
     每个阶段（Step1/2/3 + 反思环 + 评估）的进度、日志、逐轮评估
  4. 在流程中的「检查点」暂停，向使用者提问，补充额外知识 / 校验产物
     （POST /api/answer 回填答案后流程继续）；是否提问、在哪些检查点提问，均可配置
  5. POST /api/stop 中止运行

运行时真正驱动 run_pipeline.py 的编排函数跑三级链路 + 反思环。

三个可配置检查点（呼应反思环）：
  - after_compile           每轮编译出 SKILL.md 后 → 请你人工校验关键条目
  - on_gap_low_confidence   发现缺口多 / 置信档偏低时 → 请你补充素材或确认判断
  - before_reflection       进入下一轮反思前 → 请你确认是否继续、是否追加素材

  这些检查点收集到的「答案」会被并入 reflection_context，作为下一轮的补充知识注入。
"""

import base64
import binascii
import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
STATIC_DIR = Path(__file__).parent / "static"

MAX_KNOWLEDGE_FILE_BYTES = 10 * 1024 * 1024
MAX_KNOWLEDGE_UPLOAD_BYTES = 40 * 1024 * 1024
# JSON + base64 is deliberately used here because the embedded SkillMiner
# console is stdlib-only.  Leave enough headroom for base64 expansion.
MAX_KNOWLEDGE_REQUEST_BYTES = 56 * 1024 * 1024
MAX_TRAJECTORY_BENCHMARK_REQUEST_BYTES = 16 * 1024 * 1024
MAX_EDITABLE_ARTIFACT_BYTES = 2 * 1024 * 1024


def _import_legacy_runtime_data():
    """Bring forward local SkillMiner state after the package-directory rename.

    Older checkouts stored runtime data below ``skillgene/skillminer`` while
    the merged project now runs from ``teamEvolver/skillminer``.  Copy only
    files that are missing at the new location, so startup is idempotent and
    never overwrites either user uploads or generated artifacts.
    """
    legacy_root = PROJECT_ROOT.parents[1] / "skillgene" / "skillminer"
    if not legacy_root.is_dir() or legacy_root.resolve() == PROJECT_ROOT:
        return
    runtime_dirs = (
        "data", "compiled_skill", "semantic_reports", "sample_packages",
        "reflection_rounds", "run_history", "benchmark_results",
        "benchmark_sessions", "trajectory_benchmarks", "coverage_reports",
        "skill_test_results", "lift_datasets", ".knowledge_originals",
    )
    for name in runtime_dirs:
        source_root = legacy_root / name
        if not source_root.is_dir():
            continue
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            target = PROJECT_ROOT / name / source.relative_to(source_root)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in ("config.yaml", ".env", "auth.json"):
        source = legacy_root / ".hermes_home" / name
        target = PROJECT_ROOT / ".hermes_home" / name
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


_import_legacy_runtime_data()

# 复用主控脚本的编排能力（真实模式用）
sys.path.insert(0, str(PROJECT_ROOT))
import human_checkpoints as hc  # noqa: E402  具体问题生成 + 检查点表单契约
import knowledge_ingestion as ki  # noqa: E402  上传文档统一转 Markdown
import lift_integration as li  # noqa: E402  SkillMiner → LIFT 数据契约与外部运行时
import mining_jobs as mj  # noqa: E402  持久化并行挖掘任务
import run_benchmark as rb  # noqa: E402  benchmark 构建 + 跑分
import run_coverage_report as rc  # noqa: E402  复用语义单元解析
import run_pipeline as rp  # noqa: E402
import trajectory_benchmark as tb  # noqa: E402  轨迹 → 内部 Benchmark 独立入口

rst = rb.rst  # run_skill_test 模块（find_skill_to_test / deploy_test_skill 等）
SUPPORTED_KNOWLEDGE_SUFFIXES = ki.SUPPORTED_KNOWLEDGE_SUFFIXES

# 旧版运行数据迁移可能带入用户 Hermes 的 teamEvolver feed/sync hooks。
# 控制台启动即清除，确保内置 Hermes 只负责挖掘模型调用。
with contextlib.suppress(OSError, ValueError, yaml.YAMLError):
    rp.hi.sanitize_config_file(PROJECT_ROOT / ".hermes_home" / "config.yaml")

JOBS = mj.MiningJobManager(PROJECT_ROOT, start_immediately=False)
MODEL_CONFIG_LOCK = threading.RLock()


# ============================================================
# 缺口 → 具体问题：把「系统发现的缺失/证据不足」抽成一条条能回答的问题
# ------------------------------------------------------------
# 关键点（呼应用户诉求）：不是问「要不要补充材料」，而是把挖掘过程中
# 真实发现的缺口逐条转成具体问题，让使用者作答。使用者的答案再作为
# 权威领域知识注入下一轮定向补证。
# ============================================================

def _visible_files(root):
    """Return non-hidden files below *root* in deterministic order."""
    root = Path(root)
    if not root.is_dir():
        return []
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


def _input_source_detail(path):
    """Build the lightweight readiness metadata shown by the web console."""
    path = Path(path)
    files = _visible_files(path)
    persisted_ingestion = ki.read_ingestion_state(
        PROJECT_ROOT,
        path.name,
        has_documents=bool(files),
    )
    ingestion = {
        key: persisted_ingestion.get(key)
        for key in (
            "schema_version", "source_path", "batch_id", "status", "stage",
            "progress", "processed_files", "total_files", "current_file",
            "error", "started_at", "updated_at", "finished_at",
        )
    }
    return {
        "path": f"data/{path.name}",
        "document_count": len(files),
        "total_bytes": sum(file.stat().st_size for file in files),
        "ready": bool(files) and ingestion["status"] == "ready",
        "ingestion": ingestion,
    }


def _compiled_skill_details():
    """Inspect mining artifacts without involving an external eval runtime."""
    base = PROJECT_ROOT / "compiled_skill"
    rows = []
    if not base.is_dir():
        return rows
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        benchmark = skill_dir / "benchmark.jsonl"
        question_count = 0
        if benchmark.is_file():
            question_count = sum(
                1
                for line in benchmark.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            )
        rows.append({
            "name": skill_dir.name,
            "has_evaluation": (skill_dir / "EVALUATION.md").is_file(),
            "has_benchmark": benchmark.is_file(),
            "question_count": question_count,
        })
    return rows


_HISTORY_ARTIFACT_NAMES = {
    "SKILL.md": "skill",
    "EVALUATION.md": "evaluation",
    "BENCHMARK.md": "benchmark",
    "benchmark.jsonl": "benchmark",
    "benchmark_bank.json": "benchmark",
}


def _session_started_at(session_id):
    """Turn the pipeline session tag into an ISO timestamp for the UI."""
    try:
        return datetime.strptime(session_id, "%Y%m%d_%H%M%S_%f").isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def _history_artifacts(root):
    """List useful, human-readable artifacts below one archived snapshot."""
    root = Path(root)
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        kind = _HISTORY_ARTIFACT_NAMES.get(path.name)
        if kind is None and "semantic_reports" in path.parts and path.suffix.lower() == ".md":
            kind = "semantic"
        if kind is None:
            continue
        rows.append({
            "name": path.name,
            "kind": kind,
            "path": path.relative_to(PROJECT_ROOT).as_posix(),
            "size_bytes": path.stat().st_size,
            "skill_name": path.parent.name if "compiled_skill" in path.parts else "",
        })
    return rows


def _skill_summaries(artifacts):
    grouped = {}
    for artifact in artifacts:
        skill_name = artifact.get("skill_name")
        if not skill_name:
            continue
        row = grouped.setdefault(skill_name, {
            "name": skill_name,
            "has_skill": False,
            "has_evaluation": False,
            "has_benchmark": False,
            "question_count": 0,
        })
        name = artifact["name"]
        row["has_skill"] = row["has_skill"] or name == "SKILL.md"
        row["has_evaluation"] = row["has_evaluation"] or name == "EVALUATION.md"
        row["has_benchmark"] = row["has_benchmark"] or artifact["kind"] == "benchmark"
        if name == "benchmark.jsonl":
            target = (PROJECT_ROOT / artifact["path"]).resolve()
            row["question_count"] = sum(
                1 for line in target.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip()
            )
    return sorted(grouped.values(), key=lambda item: item["name"])


def list_mining_runs():
    """Build persisted mining history from round archives and final snapshots.

    ``reflection_rounds/<session>`` belongs to that session.  By contrast,
    ``run_history/<new-session>/preexisting`` is the final snapshot of the
    immediately preceding session, moved aside when the new run starts.  Merge
    it back into that preceding session so later-generated Benchmark files are
    shown beside the Skill that produced them.
    """
    rounds_root = PROJECT_ROOT / "reflection_rounds"
    history_root = PROJECT_ROOT / "run_history"
    session_ids = sorted(
        (path.name for path in rounds_root.iterdir() if path.is_dir()),
        reverse=True,
    ) if rounds_root.is_dir() else []
    runs = {}
    for session_id in session_ids:
        session_root = rounds_root / session_id
        round_dirs = sorted(
            (path for path in session_root.glob("round_*") if path.is_dir()),
            key=lambda path: int(path.name.removeprefix("round_") or 0),
        )
        rounds = []
        for round_dir in round_dirs:
            artifacts = _history_artifacts(round_dir)
            rounds.append({
                "round": int(round_dir.name.removeprefix("round_") or 0),
                "artifacts": artifacts,
                "skills": _skill_summaries(artifacts),
            })
        runs[session_id] = {
            "run_id": session_id,
            "started_at": _session_started_at(session_id),
            "status": "archived",
            "rounds": rounds,
            "final_artifacts": [],
            "skills": _skill_summaries(rounds[-1]["artifacts"] if rounds else []),
        }

    active_run_id = str((MANAGER.config or {}).get("run_id") or "")
    if not active_run_id and MANAGER.state in {"running", "waiting"} and session_ids:
        # Compatibility for a task that was started before run_id persistence
        # was added: the newest on-disk session is necessarily the active one.
        active_run_id = max(session_ids)
    if active_run_id in runs and MANAGER.state in {"running", "waiting"}:
        runs[active_run_id]["status"] = MANAGER.state

    # A preexisting snapshot at session N belongs to the latest session before N.
    for container in sorted(history_root.iterdir()) if history_root.is_dir() else []:
        snapshot = container / "preexisting"
        artifacts = _history_artifacts(snapshot)
        if not artifacts:
            continue
        preceding = next((sid for sid in sorted(runs, reverse=True) if sid < container.name), None)
        if preceding is None:
            preceding = f"legacy_before_{container.name}"
            runs[preceding] = {
                "run_id": preceding,
                "started_at": "",
                "status": "archived",
                "rounds": [],
                "final_artifacts": [],
                "skills": [],
            }
        runs[preceding]["final_artifacts"] = artifacts
        runs[preceding]["skills"] = _skill_summaries(artifacts)

    current_artifacts = _history_artifacts(PROJECT_ROOT / "compiled_skill")
    current_artifacts += _history_artifacts(PROJECT_ROOT / "semantic_reports")
    if current_artifacts:
        current_skills = _skill_summaries(current_artifacts)
        if active_run_id in runs and MANAGER.state in {"running", "waiting"}:
            runs[active_run_id]["final_artifacts"] = current_artifacts
            if current_skills:
                runs[active_run_id]["skills"] = current_skills
        else:
            runs["current"] = {
                "run_id": "current",
                "started_at": "",
                "status": MANAGER.state,
                "rounds": [],
                "final_artifacts": current_artifacts,
                "skills": current_skills,
            }
    return sorted(
        runs.values(),
        key=lambda item: (item["run_id"] == "current", item["started_at"], item["run_id"]),
        reverse=True,
    )


def read_history_artifact(relative_path):
    """Read one history artifact while preventing arbitrary file access."""
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("缺少产物路径")
    target = (PROJECT_ROOT / raw).resolve()
    allowed_roots = [
        (PROJECT_ROOT / name).resolve()
        for name in (
            "compiled_skill", "semantic_reports", "reflection_rounds", "run_history", "mining_jobs"
        )
    ]
    if not any(target == root or root in target.parents for root in allowed_roots):
        raise ValueError("产物路径不在允许的历史目录中")
    jobs_root = (PROJECT_ROOT / "mining_jobs").resolve()
    if jobs_root in target.parents and not any(
        name in target.parts
        for name in ("compiled_skill", "semantic_reports", "reflection_rounds")
    ):
        raise ValueError("任务路径不是可预览的挖掘产物")
    if not target.is_file() or target.suffix.lower() not in {".md", ".json", ".jsonl", ".txt"}:
        raise FileNotFoundError("历史产物不存在或不支持预览")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("产物超过 2 MB，暂不支持在线预览")
    return {
        "path": target.relative_to(PROJECT_ROOT).as_posix(),
        "name": target.name,
        "content": target.read_text(encoding="utf-8", errors="replace"),
        "size_bytes": target.stat().st_size,
    }


def save_history_artifact(relative_path, content):
    """Persist an approved human revision to a completed text artifact."""
    if not isinstance(content, str):
        raise ValueError("产物内容必须是文本")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_EDITABLE_ARTIFACT_BYTES:
        raise ValueError("产物超过 2 MB，暂不支持在线编辑")

    current = read_history_artifact(relative_path)
    target = (PROJECT_ROOT / current["path"]).resolve()
    is_named_artifact = target.name in _HISTORY_ARTIFACT_NAMES
    is_semantic_report = "semantic_reports" in target.parts and target.suffix.lower() == ".md"
    if not (is_named_artifact or is_semantic_report):
        raise ValueError("该文件不是可编辑的挖掘产物")

    jobs_root = (PROJECT_ROOT / "mining_jobs").resolve()
    if jobs_root in target.parents:
        relative = target.relative_to(jobs_root)
        if len(relative.parts) < 2:
            raise ValueError("挖掘任务产物路径无效")
        metadata_path = jobs_root / relative.parts[0] / "job.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法确认挖掘任务状态") from exc
        if str(metadata.get("status") or "") != "succeeded":
            raise ValueError("只有已完成任务的产物可以人工修改")

    temp = target.with_name(f".{target.name}.editing-{os.getpid()}-{threading.get_ident()}")
    try:
        temp.write_bytes(encoded)
        os.replace(temp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    result = read_history_artifact(current["path"])
    result["ok"] = True
    result["edited"] = True
    return result


def _legacy_run_as_job(run):
    """Expose pre-job-manager archives through the same task-list contract."""
    artifacts = list(run.get("final_artifacts") or [])
    rounds = list(run.get("rounds") or [])
    if not artifacts and rounds:
        artifacts = list(rounds[-1].get("artifacts") or [])
    skills = list(run.get("skills") or [])
    skill_label = "、".join(item.get("name", "") for item in skills if item.get("name"))
    status = str(run.get("status") or "archived")
    is_active = status in {"running", "waiting"}
    gap_questions = []
    seen_gaps = set()
    semantic_dirs = []
    for artifact in artifacts:
        if artifact.get("kind") != "semantic":
            continue
        target = (PROJECT_ROOT / str(artifact.get("path") or "")).resolve()
        if target.is_file() and target.parent not in semantic_dirs:
            semantic_dirs.append(target.parent)
    for semantic_dir in semantic_dirs:
        extracted, _ = hc.extract_gap_questions_from_semantic_reports(semantic_dir, limit=50)
        for question in extracted:
            key = str(question.get("qid") or question.get("question") or "")
            if key and key not in seen_gaps:
                seen_gaps.add(key)
                gap_questions.append(question)

    return {
        "job_id": f"legacy:{run.get('run_id')}",
        "legacy_run_id": run.get("run_id"),
        "legacy": True,
        "name": skill_label or "旧版挖掘任务",
        "status": "running" if is_active else "succeeded",
        "input_dir": "",
        "document_count": None,
        "max_rounds": max(1, len(rounds)),
        "current_round": len(rounds),
        "phase": {
            "step1": "done" if not is_active else "idle",
            "step2": "done" if not is_active else "idle",
            "step3": "done" if not is_active else "idle",
        },
        "created_at": run.get("started_at") or "",
        "started_at": run.get("started_at") or "",
        "finished_at": run.get("started_at") or "",
        "updated_at": run.get("started_at") or "",
        "error": "",
        "stop_reason": "历史归档",
        "artifacts": artifacts,
        "rounds": rounds,
        "logs": [],
        "skills": skills,
        "knowledge_gaps": {
            "total": len(gap_questions),
            "questions": gap_questions,
        } if gap_questions else None,
    }


def list_all_mining_jobs():
    jobs = JOBS.list_jobs()
    jobs.extend(_legacy_run_as_job(run) for run in list_mining_runs())
    return sorted(jobs, key=lambda item: item.get("created_at") or "", reverse=True)


def get_mining_job(job_id):
    if str(job_id).startswith("legacy:"):
        target = str(job_id).removeprefix("legacy:")
        run = next((item for item in list_mining_runs() if str(item.get("run_id")) == target), None)
        if run is None:
            raise KeyError(job_id)
        return _legacy_run_as_job(run)
    return JOBS.get_job(str(job_id))


def _knowledge_source_dir(source_path):
    """Resolve one direct child of data/ without allowing path traversal."""
    raw = str(source_path or "data/input").strip().replace("\\", "/").rstrip("/")
    parts = raw.split("/")
    if len(parts) != 2 or parts[0] != "data":
        raise ValueError("知识源必须是 data/ 下的一级目录")
    name = parts[1]
    if not name or name.startswith(".") or len(name) > 80:
        raise ValueError("知识源目录名无效")
    data_root = (PROJECT_ROOT / "data").resolve()
    target = (data_root / name).resolve()
    if target.parent != data_root:
        raise ValueError("知识源目录越界")
    return target


def _knowledge_originals_dir(source_dir):
    """Resolve the raw-file archive paired with one normalized data source."""
    root = (PROJECT_ROOT / ".knowledge_originals").resolve()
    target = (root / Path(source_dir).name).resolve()
    if target.parent != root:
        raise ValueError("知识源原件目录越界")
    return target


def _available_upload_path(source_dir, filename, reserved):
    """Choose a non-destructive destination, including duplicates in one batch."""
    candidate = source_dir / filename
    index = 2
    while candidate.exists() or candidate.name in reserved:
        candidate = source_dir / f"{Path(filename).stem}-{index}{Path(filename).suffix}"
        index += 1
    reserved.add(candidate.name)
    return candidate


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(target, payload):
    """Publish a new file atomically so failed writes never expose partial data."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        if target.exists():
            raise FileExistsError(f"目标文件已存在：{target.name}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file_atomic(source, target):
    """Copy one file through a hidden temporary path before publishing it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if target.exists():
            raise FileExistsError(f"目标文件已存在：{target.name}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_uploaded_knowledge(body):
    """Convert a batch to Markdown and archive originals outside pipeline input."""
    if not isinstance(body, dict):
        raise ValueError("上传参数必须是对象")
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("请至少选择一个文件")
    if len(files) > 50:
        raise ValueError("单次最多上传 50 个文件")

    source_dir = _knowledge_source_dir(body.get("source_path") or "data/input")
    create_source = body.get("create_source") is True
    requested_batch_id = str(body.get("batch_id") or "").strip()
    if requested_batch_id and (
        len(requested_batch_id) > 100
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", requested_batch_id)
    ):
        raise ValueError("上传批次标识格式无效")
    batch_id = requested_batch_id or uuid.uuid4().hex
    prepared = []
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("文件参数格式错误")
        filename = str(item.get("name") or "").strip()
        if (
            not filename
            or filename.startswith(".")
            or filename != Path(filename).name
            or "/" in filename
            or "\\" in filename
            or len(filename) > 180
        ):
            raise ValueError(f"文件名无效：{filename or '（空）'}")
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
            allowed = "、".join(sorted(SUPPORTED_KNOWLEDGE_SUFFIXES))
            raise ValueError(f"不支持 {suffix or '无扩展名'} 文件；支持：{allowed}")
        content_b64 = item.get("content_b64")
        if not isinstance(content_b64, str) or not content_b64:
            raise ValueError(f"文件内容为空：{filename}")
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"文件内容编码无效：{filename}") from exc
        if len(raw) > MAX_KNOWLEDGE_FILE_BYTES:
            raise ValueError(f"文件超过 10 MB：{filename}")
        total_bytes += len(raw)
        if total_bytes > MAX_KNOWLEDGE_UPLOAD_BYTES:
            raise ValueError("单次上传总大小不能超过 40 MB")
        prepared.append((filename, raw))

    with ki.source_operation_lock(PROJECT_ROOT, source_dir.name) as acquired:
        if not acquired:
            raise ValueError(f"知识源 {source_dir.name} 正在处理，请等待完成后再上传")
        originals_dir = _knowledge_originals_dir(source_dir)
        if create_source:
            if source_dir.exists() or originals_dir.exists():
                raise ValueError(f"知识源已存在：{source_dir.name}")
        elif not source_dir.is_dir():
            raise ValueError(f"知识源不存在：{source_dir.name}")
        legacy_manager = globals().get("MANAGER")
        if legacy_manager is not None and legacy_manager.state in ("running", "waiting"):
            raise ValueError("挖掘任务运行中，暂不能修改输入文档")

        started_at = datetime.now().astimezone().isoformat()
        state_base = {
            "batch_id": batch_id,
            "total_files": len(prepared),
            "processed_files": 0,
            "current_file": "",
            "error": "",
            "started_at": started_at,
            "finished_at": "",
            "owner_pid": os.getpid(),
        }
        normalized = []
        created_paths = []
        try:
            ki.write_ingestion_state(
                PROJECT_ROOT,
                source_dir.name,
                **state_base,
                status="processing",
                stage="validating",
                progress=2,
            )
            for index, (filename, raw) in enumerate(prepared):
                ki.write_ingestion_state(
                    PROJECT_ROOT,
                    source_dir.name,
                    status="processing",
                    stage="converting",
                    progress=5 + round(65 * index / len(prepared)),
                    processed_files=index,
                    current_file=filename,
                )
                converted = ki.normalize_knowledge_document(filename, raw)
                normalized.append((filename, raw, converted))
                ki.write_ingestion_state(
                    PROJECT_ROOT,
                    source_dir.name,
                    status="processing",
                    stage="converting",
                    progress=5 + round(65 * (index + 1) / len(prepared)),
                    processed_files=index + 1,
                    current_file=filename,
                )

            source_dir.mkdir(parents=True, exist_ok=True)
            originals_dir.mkdir(parents=True, exist_ok=True)
            reserved_markdown = set()
            reserved_originals = set()
            write_plan = []
            pending_outputs = []
            for filename, raw, converted in normalized:
                normalized_name = f"{Path(filename).stem}.md"
                target = _available_upload_path(source_dir, normalized_name, reserved_markdown)
                original_target = _available_upload_path(originals_dir, filename, reserved_originals)
                encoded_markdown = converted.markdown.encode("utf-8")
                write_plan.append((
                    filename,
                    raw,
                    converted,
                    normalized_name,
                    target,
                    original_target,
                    encoded_markdown,
                ))
                pending_outputs.extend((
                    {
                        "path": original_target.relative_to(PROJECT_ROOT).as_posix(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                    {
                        "path": target.relative_to(PROJECT_ROOT).as_posix(),
                        "sha256": hashlib.sha256(encoded_markdown).hexdigest(),
                    },
                ))
            ki.write_ingestion_state(
                PROJECT_ROOT,
                source_dir.name,
                status="processing",
                stage="writing",
                progress=72,
                processed_files=len(normalized),
                current_file="",
                pending_outputs=pending_outputs,
            )
            written = []
            for index, (
                filename,
                raw,
                converted,
                normalized_name,
                target,
                original_target,
                encoded_markdown,
            ) in enumerate(write_plan):
                ki.write_ingestion_state(
                    PROJECT_ROOT,
                    source_dir.name,
                    status="processing",
                    stage="writing",
                    progress=72 + round(25 * index / len(normalized)),
                    processed_files=len(normalized),
                    current_file=filename,
                )
                _write_bytes_atomic(original_target, raw)
                created_paths.append(original_target)
                _write_bytes_atomic(target, encoded_markdown)
                created_paths.append(target)
                written.append({
                    "name": target.name,
                    "path": f"data/{source_dir.name}/{target.name}",
                    "original_name": filename,
                    "size_bytes": len(raw),
                    "normalized_size_bytes": len(encoded_markdown),
                    "renamed": target.name != normalized_name,
                    "converted": Path(filename).suffix.lower() != ".md",
                    "source_format": converted.source_format,
                    "source_encoding": converted.source_encoding,
                })

            ki.write_ingestion_state(
                PROJECT_ROOT,
                source_dir.name,
                status="ready",
                stage="complete",
                progress=100,
                processed_files=len(normalized),
                total_files=len(normalized),
                current_file="",
                error="",
                finished_at=datetime.now().astimezone().isoformat(),
                pending_outputs=[],
                owner_pid=0,
            )
            return {
                "ok": True,
                "batch_id": batch_id,
                "written": written,
                "source": _input_source_detail(source_dir),
            }
        except Exception as exc:
            for created in reversed(created_paths):
                with contextlib.suppress(OSError):
                    created.unlink()
            if create_source:
                with contextlib.suppress(OSError):
                    shutil.rmtree(source_dir)
                with contextlib.suppress(OSError):
                    shutil.rmtree(originals_dir)
            else:
                ki.write_ingestion_state(
                    PROJECT_ROOT,
                    source_dir.name,
                    status="failed",
                    stage="failed",
                    progress=0,
                    current_file="",
                    error=str(exc)[:500],
                    finished_at=datetime.now().astimezone().isoformat(),
                    pending_outputs=[],
                    owner_pid=0,
                )
            raise


def create_knowledge_source(body):
    """Create one empty first-level directory below data/."""
    if not isinstance(body, dict):
        raise ValueError("创建参数必须是对象")
    raw_name = str(body.get("name") or body.get("source_name") or "").strip()
    source_dir = _knowledge_source_dir(f"data/{raw_name}")
    with ki.source_operation_lock(PROJECT_ROOT, source_dir.name) as acquired:
        if not acquired:
            raise ValueError("知识源正在创建或后处理，请稍后重试")
        if source_dir.exists():
            raise ValueError(f"知识源已存在：{raw_name}")
        source_dir.mkdir(parents=True)
        return {"ok": True, "source": _input_source_detail(source_dir)}


def rename_knowledge_source(source_name, body):
    """Rename one data source without overwriting an existing directory."""
    if not isinstance(body, dict):
        raise ValueError("重命名参数必须是对象")
    source_dir = _knowledge_source_dir(f"data/{str(source_name or '').strip()}")
    if not source_dir.is_dir():
        raise FileNotFoundError(f"知识源不存在：{source_dir.name}")

    raw_name = str(body.get("name") or body.get("source_name") or "").strip()
    target_dir = _knowledge_source_dir(f"data/{raw_name}")
    lock_names = sorted({source_dir.name, target_dir.name})
    with contextlib.ExitStack() as stack:
        for name in lock_names:
            if not stack.enter_context(ki.source_operation_lock(PROJECT_ROOT, name)):
                raise ValueError("知识源正在后处理，完成前不能重命名")
        source_detail = _input_source_detail(source_dir)
        if source_detail["ingestion"]["status"] == "processing":
            raise ValueError("知识源正在后处理，完成前不能重命名")
        if target_dir.name == source_dir.name:
            return {
                "ok": True,
                "previous_path": f"data/{source_dir.name}",
                "source": source_detail,
            }

        conflict = next((
            item for item in source_dir.parent.iterdir()
            if item.is_dir()
            and item != source_dir
            and item.name.casefold() == target_dir.name.casefold()
        ), None)
        target_is_source = (
            target_dir.exists()
            and os.path.samefile(source_dir, target_dir)
        )
        if conflict is not None or (target_dir.exists() and not target_is_source):
            existing_name = conflict.name if conflict is not None else target_dir.name
            raise FileExistsError(f"知识源名称已存在：{existing_name}")

        source_originals = _knowledge_originals_dir(source_dir)
        target_originals = _knowledge_originals_dir(target_dir)
        if source_originals.is_dir() and target_originals.exists():
            raise FileExistsError(f"知识源原件归档已存在：{target_dir.name}")

        previous_path = f"data/{source_dir.name}"
        source_dir.rename(target_dir)
        if source_originals.is_dir():
            target_originals.parent.mkdir(parents=True, exist_ok=True)
            source_originals.rename(target_originals)
        return {
            "ok": True,
            "previous_path": previous_path,
            "source": _input_source_detail(target_dir),
        }


def delete_knowledge_source(source_name):
    """Delete exactly one selected data source directory."""
    source_dir = _knowledge_source_dir(f"data/{str(source_name or '').strip()}")
    if not source_dir.is_dir():
        raise FileNotFoundError(f"知识源不存在：{source_dir.name}")
    with ki.source_operation_lock(PROJECT_ROOT, source_dir.name) as acquired:
        if not acquired:
            raise ValueError("知识源正在后处理，完成前不能删除")
        detail = _input_source_detail(source_dir)
        if detail["ingestion"]["status"] == "processing":
            raise ValueError("知识源正在后处理，完成前不能删除")
        shutil.rmtree(source_dir)
        originals_dir = _knowledge_originals_dir(source_dir)
        if originals_dir.is_dir():
            shutil.rmtree(originals_dir)
        return {"ok": True, "deleted": detail}


def _merge_destination(source_dir, relative_path, reserved=None):
    """Resolve a collision-free target while preserving nested structure."""
    relative_path = Path(relative_path)
    target = source_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    index = 2
    reserved = reserved if reserved is not None else set()
    while target.exists() or target.relative_to(source_dir).as_posix() in reserved:
        target = target.with_name(f"{relative_path.stem}-{index}{relative_path.suffix}")
        index += 1
    reserved.add(target.relative_to(source_dir).as_posix())
    return target


def merge_knowledge_sources(body):
    """Copy two or more existing sources into a new or existing target source."""
    if not isinstance(body, dict):
        raise ValueError("合并参数必须是对象")
    raw_sources = body.get("source_paths") or body.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) < 2:
        raise ValueError("请至少选择两个知识源进行合并")
    sources = []
    seen = set()
    for raw in raw_sources:
        path = _knowledge_source_dir(str(raw or ""))
        if path.name in seen:
            continue
        if not path.is_dir():
            raise FileNotFoundError(f"知识源不存在：{path.name}")
        seen.add(path.name)
        sources.append(path)
    if len(sources) < 2:
        raise ValueError("请至少选择两个不同的知识源")

    target_name = str(body.get("target_name") or "").strip()
    target = _knowledge_source_dir(f"data/{target_name}")
    if target.name in seen:
        raise ValueError("合并目标不能与来源知识源同名")
    lock_names = sorted({target.name, *(source.name for source in sources)})
    with contextlib.ExitStack() as stack:
        for name in lock_names:
            if not stack.enter_context(ki.source_operation_lock(PROJECT_ROOT, name)):
                raise ValueError("所选知识源正在后处理，完成前不能合并")
        for source in sources:
            if not _input_source_detail(source)["ready"]:
                raise ValueError(f"知识源尚未就绪，不能合并：{source.name}")
        if target.exists():
            target_status = _input_source_detail(target)["ingestion"]["status"]
            if target_status not in {"empty", "ready"}:
                raise ValueError(f"合并目标尚未就绪：{target.name}")

        total_items = sum(len(_visible_files(source)) for source in sources)
        batch_id = f"merge-{uuid.uuid4().hex}"
        ki.write_ingestion_state(
            PROJECT_ROOT,
            target.name,
            batch_id=batch_id,
            status="processing",
            stage="merging",
            progress=2,
            processed_files=0,
            total_files=total_items,
            current_file="",
            error="",
            started_at=datetime.now().astimezone().isoformat(),
            finished_at="",
            owner_pid=os.getpid(),
        )
        target_preexisted = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        created_paths = []
        processed = 0
        try:
            data_plan = []
            original_plan = []
            pending_outputs = []
            reserved_data = set()
            reserved_originals = set()
            for source in sources:
                for item in _visible_files(source):
                    relative = item.relative_to(source)
                    destination = _merge_destination(target, relative, reserved_data)
                    data_plan.append((source, item, relative, destination))
                    pending_outputs.append({
                        "path": destination.relative_to(PROJECT_ROOT).as_posix(),
                        "sha256": _file_sha256(item),
                    })
                originals = _knowledge_originals_dir(source)
                if originals.is_dir():
                    target_originals = _knowledge_originals_dir(target)
                    for item in _visible_files(originals):
                        relative = Path(source.name) / item.relative_to(originals)
                        destination = _merge_destination(
                            target_originals,
                            relative,
                            reserved_originals,
                        )
                        original_plan.append((item, destination))
                        pending_outputs.append({
                            "path": destination.relative_to(PROJECT_ROOT).as_posix(),
                            "sha256": _file_sha256(item),
                        })
            ki.write_ingestion_state(
                PROJECT_ROOT,
                target.name,
                status="processing",
                stage="merging",
                progress=5,
                pending_outputs=pending_outputs,
            )

            for source, item, relative, destination in data_plan:
                _copy_file_atomic(item, destination)
                created_paths.append(destination)
                processed += 1
                copied.append({
                    "from": f"data/{source.name}/{relative.as_posix()}",
                    "path": f"data/{target.name}/{destination.relative_to(target).as_posix()}",
                    "renamed": destination.relative_to(target) != relative,
                })
                ki.write_ingestion_state(
                    PROJECT_ROOT,
                    target.name,
                    status="processing",
                    stage="merging",
                    progress=5 + round(90 * processed / max(1, total_items)),
                    processed_files=processed,
                    current_file=item.name,
                )
            for item, destination in original_plan:
                _copy_file_atomic(item, destination)
                created_paths.append(destination)
            ki.write_ingestion_state(
                PROJECT_ROOT,
                target.name,
                status="ready",
                stage="complete",
                progress=100,
                processed_files=total_items,
                total_files=total_items,
                current_file="",
                error="",
                finished_at=datetime.now().astimezone().isoformat(),
                pending_outputs=[],
                owner_pid=0,
            )
            return {
                "ok": True,
                "sources": [f"data/{source.name}" for source in sources],
                "source": _input_source_detail(target),
                "copied": copied,
            }
        except Exception as exc:
            for created in reversed(created_paths):
                with contextlib.suppress(OSError):
                    created.unlink()
            if not target_preexisted:
                with contextlib.suppress(OSError):
                    shutil.rmtree(target)
            ki.write_ingestion_state(
                PROJECT_ROOT,
                target.name,
                status="failed",
                stage="failed",
                progress=0,
                current_file="",
                error=str(exc)[:500],
                finished_at=datetime.now().astimezone().isoformat(),
                pending_outputs=[],
                owner_pid=0,
            )
            raise


def _find_compiled_skill(skill_name=""):
    """Resolve one compiled skill without allowing paths outside compiled_skill/."""
    if not skill_name:
        return rst.find_skill_to_test()
    base = (PROJECT_ROOT / "compiled_skill").resolve()
    target = (base / str(skill_name)).resolve()
    if target.parent != base or not (target / "SKILL.md").is_file():
        return None
    return target


def extract_gap_questions_from_skill(skill_md_path):
    """从 SKILL.md 里抽出「缺口/冲突/存疑」条目，整理成具体问题清单。

    SKILL.md 的能力维度小节里，缺口通常写成：
      - **高严重度缺口**：一般/重大之间的判定边界缺失——延误多久算"重大"？…（来源：…）
      - **冲突未解决**：情绪安抚要求充分倾听，可能与时效冲突——优先哪个？（来源：…）
    我们按 `### 维度X：标题` 归属维度，逐条抽成 {qid,dimension,severity,question}。
    """
    return hc.extract_gap_questions_from_skill(skill_md_path)


# ============================================================
# 语义归纳结果 → 关键知识缺口补证问题
# ------------------------------------------------------------
# Step2（语义发现）产出 semantic_reports/*.md，内含结构化缺口清单 GAP-XX。
# 检查点只询问会阻碍 Skill 落地的缺失规则、阈值、公式和例外；使用者
# 的回答会作为编译（Step3）前的权威领域知识注入 reflection_context。
# ============================================================
def semantic_gap_questions(semantic_reports_dir, limit=10):
    """从语义报告的结构化缺口清单生成逐项补证问题。"""
    return hc.extract_gap_questions_from_semantic_reports(semantic_reports_dir, limit=limit)


# ============================================================
# 事件总线：一个运行进程内的 SSE 广播
# ============================================================
class EventBus:
    """把编排线程产生的事件广播给所有已连接的 SSE 客户端。

    每个事件带进程内单调递增的 seq（跨 reset 不回卷），用于：
      - SSE 的 `id:` 字段 + Last-Event-ID 断点续传（浏览器自动重连不再全量重放）
      - 前端按 seq 去重（防御手动重连等场景）
    """

    MAX_HISTORY = 2000  # 历史上限：防止长跑后新连接重放巨量事件

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()
        self._history = []  # 便于新连接补发已发生的事件
        self._seq = 0
        self._stream_id = f"{time.time_ns():x}"

    def subscribe(self, after_seq=0):
        """订阅事件流；只补发 seq > after_seq 的历史（after_seq=0 即全部）。"""
        q = queue.Queue(maxsize=self.MAX_HISTORY)
        with self._lock:
            for ev in self._history:
                if ev.get("seq", 0) > after_seq:
                    q.put(ev)
            self._subscribers.append(q)
        return q

    @property
    def stream_id(self):
        return self._stream_id

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event):
        with self._lock:
            self._seq += 1
            event["seq"] = self._seq
            event["stream_id"] = self._stream_id
            self._history.append(event)
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY:]
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # 慢客户端只丢弃其最旧事件，不让队列无限增长拖垮运行进程。
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait(event)

    def reset(self):
        with self._lock:
            self._history = []


# ============================================================
# 运行管理器：单例，持有当前运行状态、编排线程、提问阻塞队列
# ============================================================
class RunManager:
    def __init__(self):
        self.bus = EventBus()
        self.state = "idle"          # idle / running / waiting / done / error
        self.thread = None
        self.stop_flag = threading.Event()
        self.answer_q = queue.Queue()   # 检查点提问的答案回填
        self.pending_question = None
        self.config = {}
        self.task_kind = ""
        self.last_result = None
        self._lock = threading.Lock()

    # --- 事件便捷方法 ---
    def emit(self, etype, **payload):
        payload["type"] = etype
        payload["ts"] = datetime.now().strftime("%H:%M:%S")
        self.bus.publish(payload)

    def log(self, msg, level="info"):
        self.emit("log", level=level, msg=msg)

    # --- 生命周期 ---
    def start(self, config):
        with self._lock:
            if self.state in ("running", "waiting"):
                return False, "已有任务在运行中"
            input_abs = (PROJECT_ROOT / str(config.get("input_dir") or "data/input")).resolve()
            if input_abs != PROJECT_ROOT and PROJECT_ROOT not in input_abs.parents:
                return False, "输入目录必须位于 SkillMiner 项目内"
            if not input_abs.is_dir():
                return False, f"输入目录不存在：{config.get('input_dir') or 'data/input'}"
            input_files = _visible_files(input_abs)
            if not input_files:
                return False, "输入目录中没有可用于挖掘的文档"
            data_root = (PROJECT_ROOT / "data").resolve()
            source_name = input_abs.name if input_abs.parent == data_root else ""
            lock_context = (
                ki.source_operation_lock(PROJECT_ROOT, source_name)
                if source_name else contextlib.nullcontext(True)
            )
            with lock_context as acquired:
                if not acquired:
                    return False, "知识源正在后处理，完成前不能用于挖掘"
                if source_name:
                    ingestion = ki.read_ingestion_state(
                        PROJECT_ROOT,
                        source_name,
                        has_documents=bool(input_files),
                    )
                    if ingestion["status"] == "processing":
                        return False, "知识源正在后处理，完成前不能用于挖掘"
                    if ingestion["status"] == "failed":
                        return False, f"知识源后处理失败：{ingestion.get('error') or '请重新上传文件'}"
                self.bus.reset()
                self.stop_flag.clear()
                # 清空可能残留的旧答案
                while not self.answer_q.empty():
                    self.answer_q.get_nowait()
                self.config = config
                self.task_kind = "skill_mining"
                self.last_result = None
                self.state = "running"
                self.pending_question = None
                # reset 事件：通知所有已连接客户端清空面板（新任务开始）
                self.emit("reset", scope="pipeline")
                self.thread = threading.Thread(target=self._run, args=(config,), daemon=True)
                self.thread.start()
                return True, "started"

    def submit_answer(self, payload):
        # 用 _lock 保护「读 state/pending_question + 校验」这一复合判断，避免与
        # worker 线程切换状态时产生 check-then-act 竞态。注意：queue.put 放在锁外，
        # 且本方法不阻塞——ask_questions 里阻塞的 answer_q.get() 全程不持锁，故不会死锁。
        with self._lock:
            pending = self.pending_question
            if self.state != "waiting" or not pending:
                return False, "当前没有待回答的提问"
            # 必须回答「当前」这个提问：拒绝历史重放出的旧弹窗/其他页面的陈旧提交，
            # 防止陈旧答案滞留队列、被下一个不相干的检查点消费。
            if payload.get("question_id") != pending["id"]:
                return False, "提问已过期（question_id 不匹配），请刷新页面后重试"
        self.answer_q.put(payload)
        return True, "ok"

    def request_stop(self):
        self.stop_flag.set()
        # 立即终止在跑的 hermes 子进程，而不是等当前模型调用自然结束
        killed = rp.terminate_active_procs()
        if killed:
            self.log(f"■ 已终止 {killed} 个在跑的 hermes 子进程", level="warn")
        # 若正卡在提问上，塞一个停止信号让它解阻塞
        if self.state == "waiting":
            self.answer_q.put({"_stopped": True})
        return True, "stopping"

    def snapshot(self):
        return {"state": self.state, "pending_question": self.pending_question,
                "config": self.config, "task_kind": self.task_kind,
                "last_result": self.last_result}

    # --- 检查点提问：列出一组「具体问题」，阻塞直到前端逐条回答或中止 ---
    # questions: [{qid, dimension, severity, question}]
    # 返回 (answer_map: {qid: text}, stopped: bool)
    def ask_questions(self, checkpoint, round_idx, title, intro, questions,
                      allow_stop=False):
        if not questions:
            return {}, False
        qid = f"{checkpoint}-r{round_idx}-{int(time.time())}"
        # 发问前排空队列：防御上一个提问竞态期间残留的陈旧 payload
        while not self.answer_q.empty():
            try:
                self.answer_q.get_nowait()
            except queue.Empty:
                break
        # pending_question 与 state 成对切换，用 _lock 保证 submit_answer 侧看到的是
        # 一致快照（要么都已就绪、要么都未就绪），避免半初始化窗口内的误判。
        with self._lock:
            self.pending_question = {
                "id": qid, "checkpoint": checkpoint, "round": round_idx,
                "title": title, "intro": intro,
                "questions": questions, "allow_stop": allow_stop,
            }
            self.state = "waiting"
        self.emit("question", **self.pending_question)
        self.emit("status", state="waiting")
        # 阻塞等待答案；丢弃 question_id 不匹配的陈旧 payload（submit_answer 已挡一层，
        # 这里是竞态兜底），只接受停止信号或本提问的答案
        while True:
            payload = self.answer_q.get()
            if isinstance(payload, dict) and payload.get("_stopped"):
                break
            if isinstance(payload, dict) and payload.get("question_id") == qid:
                break
        with self._lock:
            self.pending_question = None
            self.state = "running"
        self.emit("status", state="running")
        if payload.get("_stopped"):
            # 中止也要发 answer_ack：与 question 事件成对，避免历史重放时
            # 留下无配对的提问，导致重连客户端弹出「幽灵弹窗」
            self.emit("answer_ack", checkpoint=checkpoint,
                      answered=0, total=len(questions), stopped=True)
            return {}, True
        answers = payload.get("answers", {}) or {}
        stopped = bool(payload.get("stop"))
        answered = {k: v.strip() for k, v in answers.items() if v and v.strip()}
        self.emit("answer_ack", checkpoint=checkpoint,
                  answered=len(answered), total=len(questions))
        return answered, stopped

    @staticmethod
    def _format_qa_context(header, questions, answers):
        """把「问题→使用者回答」拼成注入下一轮的补充知识块。"""
        return hc.format_qa_context(header, questions, answers)

    # ========================================================
    # 编排主体
    # ========================================================
    def _run(self, config):
        try:
            self.emit("status", state="running")
            self.log("运行开始")
            self._run_real(config)
        except Exception as e:
            self.state = "error"
            self.emit("error", msg=f"运行异常：{e}")
            self.log(f"运行异常：{e}", level="error")
            # 终态 status：前端据此把界面从「运行中」切回可再次开始，
            # 否则任何未捕获异常都会让控制台永久卡在 running。
            self.emit("status", state="error")
        finally:
            if self.state != "error":
                self.state = "done"

    # ---- 检查点编排 ----
    # 每个检查点都：从「本轮发现的缺口/冲突」里挑出**具体问题**问使用者，
    # 使用者的回答拼成补充知识注入下一轮。gap_questions 由调用方按轮次准备。
    def _checkpoint_after_semantic(self, cfg, round_idx, semantic_reports_dir):
        """语义归纳后补证：询问 Step2 发现的关键业务知识缺口。

        返回注入 Step3 编译的补充上下文（把使用者答案当作权威领域知识）。
        """
        if not (cfg["ask_enabled"] and cfg["checkpoints"].get("after_semantic")):
            return ""
        picked, total = semantic_gap_questions(semantic_reports_dir)
        if not picked:
            return ""
        answers, _ = self.ask_questions(
            "after_semantic", round_idx,
            title=f"第 {round_idx} 轮发现 {total} 个关键知识缺口 · 请补全其中 {len(picked)} 项",
            intro="系统只列出会影响 Skill 生成、且现有素材没有给出明确答案的问题。"
                  "请填写准确规则、数值、单位、适用条件或例外；暂时无法确认的条目可以留空。",
            questions=picked,
        )
        if answers:
            self.log(f"[知识补证] 使用者补全了 {len(answers)}/{len(picked)} 个关键缺口")
        return self._format_qa_context(
            f"【使用者对第{round_idx}轮关键知识缺口的补充（编译前，具最高优先级）】",
            picked, answers)

    def _checkpoint_after_compile(self, cfg, round_idx, info, gap_questions):
        """编译后校验：挑出「证据冲突 / 存疑」类问题，请使用者以实际规则裁定。"""
        if not (cfg["ask_enabled"] and cfg["checkpoints"].get("after_compile")):
            return ""
        picked = [q for q in gap_questions
                  if any(k in q["question"] for k in ("冲突", "存疑", "一致", "优先"))][:4]
        if not picked:
            return ""
        answers, _ = self.ask_questions(
            "after_compile", round_idx,
            title=f"第 {round_idx} 轮编译完成 · 请校验以下存在冲突/存疑的判断",
            intro="系统在核对本轮 skill 时发现下列判断点在素材中无定论或相互冲突，"
                  "需要你按组织的实际规则给出裁定（逐条回答，可留空跳过）：",
            questions=picked,
        )
        if answers:
            for q in picked:
                if answers.get(q["qid"]):
                    self.log(f"[校验裁定] {q['question'][:24]}… → {answers[q['qid']]}")
        return self._format_qa_context(
            f"【使用者对冲突/存疑判断的裁定（第{round_idx}轮编译后）】", picked, answers)

    def _checkpoint_gap_low_conf(self, cfg, round_idx, info, gap_questions):
        """缺口/低置信：把发现的知识缺口逐条变成问题，请使用者补充具体规则/数值。"""
        if not (cfg["ask_enabled"] and cfg["checkpoints"].get("on_gap_low_confidence")):
            return ""
        if info["is_production"]:
            return ""  # 已生产级不触发
        picked = gap_questions[:6]
        if not picked:
            return ""
        answers, _ = self.ask_questions(
            "on_gap_low_confidence", round_idx,
            title=f"发现 {len(picked)} 处知识缺口（置信档：{info['confidence']}）· 请补充",
            intro="系统在挖掘/找证据的过程中发现下列内容在素材里缺失或证据不足，"
                  "无法自行确定。请逐条补充你掌握的准确规则/数值/来源（可留空跳过）：",
            questions=picked,
        )
        if answers:
            self.log(f"[缺口补充] 使用者回答了 {len(answers)}/{len(picked)} 条缺口问题")
        return self._format_qa_context(
            f"【使用者补充的领域知识（针对第{round_idx}轮缺口）】", picked, answers)

    def _checkpoint_before_reflection(self, cfg, round_idx, info, gap_questions):
        """反思前：请使用者指定下一轮优先攻关方向；可勾选停止。返回 (should_continue, ctx)。"""
        if not (cfg["ask_enabled"] and cfg["checkpoints"].get("before_reflection")):
            return True, ""
        remaining = "、".join(sorted(info["gap_ids"])) if info.get("gap_ids") else "若干"
        q = [{
            "qid": "priority",
            "dimension": "",
            "severity": "",
            "question": f"请问，第 {round_idx + 1} 轮应优先补证哪些缺口？",
            "context": f"当前共 {info['gap_count']} 项缺口：{remaining}",
            "field_label": "优先缺口与可用线索",
            "placeholder": "例如：优先 GAP-01、GAP-03；可参考退款规则第 4 条与 2026 年 SOP",
            "answer_type": "long_text",
            "required": False,
        }]
        answers, stopped = self.ask_questions(
            "before_reflection", round_idx,
            title=f"第 {round_idx} 轮结束 · 进入下一轮反思前",
            intro="请为下一轮定向补证指定优先级（可留空按默认顺序）；"
                  "若认为已无需继续，可勾选下方「结束反思环」。",
            questions=q, allow_stop=True,
        )
        if stopped or self.stop_flag.is_set():
            self.log("使用者选择在反思前结束反思环。")
            return False, ""
        ctx = self._format_qa_context(
            f"【使用者为第{round_idx + 1}轮指定的优先攻关方向】", q, answers)
        return True, ctx

    # ========================================================
    # real：真正驱动 run_pipeline 的编排（带检查点）
    # ========================================================
    def _run_real(self, cfg):
        # 覆盖输入目录
        rel = cfg["input_dir"].rstrip("/") + "/"
        input_abs = (PROJECT_ROOT / rel).resolve()
        # 容器校验：input_dir 必须落在项目根内，拒绝 ../ 逃逸（防路径穿越）。
        # 仅 .exists() 无法阻止 "../../../etc" 之类逃到项目外的目录。
        if input_abs != PROJECT_ROOT and PROJECT_ROOT not in input_abs.parents:
            self.emit("error", msg=f"输入目录越界（必须位于项目内）：{cfg['input_dir']}")
            self.log(f"拒绝越界输入目录：{cfg['input_dir']}", level="error")
            self.state = "error"
            self.emit("status", state="error")
            return
        if not input_abs.exists():
            self.emit("error", msg=f"输入目录不存在：{input_abs}")
            self.state = "error"
            self.emit("status", state="error")
            return
        rp.PROMPT_MODULES[0]["input_dir"] = rel

        # 把 rp 内部 print 转发到事件流
        bus_writer = _BusWriter(self)
        with contextlib.redirect_stdout(bus_writer):
            if not rp.check_hermes_installed():
                self.emit("error", msg="未找到 hermes 可执行文件")
                self.state = "error"
                self.emit("status", state="error")
                return
            rp.ensure_hermes_home()
            hermes_env, has_key = rp.build_hermes_env()
            self.log("✓ 已解析 ARK_API_KEY" if has_key else "⚠️ 未找到 ARK_API_KEY")
            if not rp.test_model_connection(hermes_env):
                self.emit("error", msg="模型连接测试失败，请检查凭据、额度、网络和 Hermes 模型配置")
                self.state = "error"
                self.emit("status", state="error")
                return
            if not rp.deploy_skills():
                self.emit("error", msg="流水线 Agent Skill 部署失败，请检查安装包是否完整")
                self.state = "error"
                self.emit("status", state="error")
                return

        max_rounds = cfg["max_rounds"]
        session_tag = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.config["run_id"] = session_tag
        with contextlib.redirect_stdout(bus_writer):
            try:
                rp.prepare_run_workspace(session_tag)
            except FileExistsError:
                self.emit("error", msg=f"运行标记冲突，请重试：{session_tag}")
                self.state = "error"
                self.emit("status", state="error")
                return
        prev_gap = None
        stop_reason = None
        final_info = None
        carry_ctx = ""

        for round_idx in range(1, max_rounds + 1):
            if self.stop_flag.is_set():
                stop_reason = "使用者手动中止"
                break
            self.emit("round_start", round=round_idx, total=max_rounds)
            self.log(f"===== 反思环 第 {round_idx}/{max_rounds} 轮 =====")

            # 反思上下文 = 缺口回跳上下文 + 使用者补充
            reflection_context = rp.build_reflection_context(
                {"gap_ids": prev_gap[1], "gap_count": len(prev_gap[1]),
                 "confidence": prev_gap[2]} if prev_gap else None,
                round_idx,
            ) + carry_ctx

            # phase 事件由 run_pipeline_once 的 on_phase 回调在真实步骤边界发出
            # （step1/2/3 各自的 active/done 共 6 个转换，流程图随实际进度点亮）
            def _on_phase(phase, state):
                self.emit("phase", phase=phase, round=round_idx, state=state)

            # 关键知识补证检查点：作为回调注入 run_pipeline_once，在 Step2 完成、
            # Step3 编译开始前触发，把结构化缺口交使用者逐项补全。答案并入
            # 本轮编译的 reflection_context。
            def _semantic_hook(r_idx, sem_dir):
                # ask_questions 会阻塞在提问上，需脱离 stdout 重定向以正常走事件流
                ctx = ""
                with contextlib.redirect_stdout(sys.__stdout__):
                    ctx = self._checkpoint_after_semantic(cfg, r_idx, sem_dir)
                return ctx

            with contextlib.redirect_stdout(bus_writer):
                ok = rp.run_pipeline_once(hermes_env, round_idx, reflection_context,
                                          after_semantic_hook=_semantic_hook,
                                          should_stop=self.stop_flag.is_set,
                                          on_phase=_on_phase)
            if not ok:
                if self.stop_flag.is_set():
                    stop_reason = "使用者手动中止"
                    break
                # 步骤失败（多为 hermes 调用失败/超时/输出异常）不立即中断本轮——
                # 流水线设计成带缺口继续、靠反思环补救。但要用结构化 warning 事件
                # 显式上报，避免失败只淹没在日志里、用户误以为一切正常。
                self.log("⚠️ 本轮部分步骤失败（详见日志）", level="warn")
                self.emit("warning", scope="pipeline", round=round_idx,
                          msg="本轮部分步骤失败（hermes 调用失败/超时/输出异常），"
                              "将带缺口继续并尝试反思补救")

            with contextlib.redirect_stdout(bus_writer):
                rp.archive_round(round_idx, session_tag)
                skill_md = rp.find_compiled_skill_md()
                info = rp.parse_skill_confidence(skill_md)
            final_info = info
            self.log(f"【本轮评估】置信档={info['confidence']} | 缺口数={info['gap_count']}")

            # 从本轮真实 SKILL.md 抽出缺口/冲突条目 → 具体问题清单。
            # round_eval 每轮只发一次（先算完 question_count 再发），前端逐轮卡片不重复
            with contextlib.redirect_stdout(bus_writer):
                gap_questions = extract_gap_questions_from_skill(skill_md)
            self.emit("round_eval", round=round_idx, confidence=info["confidence"],
                      gap_count=info["gap_count"], gaps=sorted(info["gap_ids"]),
                      question_count=len(gap_questions))

            ctx_a = self._checkpoint_after_compile(cfg, round_idx, info, gap_questions)
            if self.stop_flag.is_set():
                stop_reason = "使用者手动中止"
                break

            if info["is_production"]:
                stop_reason = "已达生产级，反思环收敛"
                break
            if skill_md is None or info["confidence"] == "unknown":
                stop_reason = "未解析到有效 SKILL.md/置信档，停止"
                break
            if round_idx >= max_rounds:
                stop_reason = f"达到最大轮数 {max_rounds}，停止"
                break
            if prev_gap is not None and info["gap_count"] >= len(prev_gap[1]):
                stop_reason = "缺口数未下降，判定收敛，停止"
                break

            ctx_b = self._checkpoint_gap_low_conf(cfg, round_idx, info, gap_questions)
            if self.stop_flag.is_set():
                stop_reason = "使用者手动中止"
                break

            with contextlib.redirect_stdout(bus_writer):
                has_supp = rp.has_supplementary_data()
            if not has_supp and not ctx_b:
                stop_reason = "无补充素材可用（增量闸门关闭），停止"
                break

            cont, ctx_c = self._checkpoint_before_reflection(cfg, round_idx, info, gap_questions)
            if self.stop_flag.is_set():
                stop_reason = "使用者手动中止"
                break
            if not cont:
                stop_reason = "使用者在反思前选择停止"
                break

            carry_ctx = ctx_a + ctx_b + ctx_c
            prev_gap = (round_idx, info["gap_ids"], info["confidence"])
            self.log(f"↻ 触发反思：带着 {info['gap_count']} 项缺口回跳重跑")

        self._finish(stop_reason or "运行结束", final_info)

    def _finish(self, stop_reason, final_info):
        payload = {"stop_reason": stop_reason}
        if final_info:
            payload["final"] = {"confidence": final_info["confidence"],
                                "gap_count": final_info["gap_count"]}
        self.emit("done", **payload)
        self.emit("status", state="done")
        self.log(f"✓ 运行结束：{stop_reason}")

    # ========================================================
    # Benchmark 构建 + 跑分（独立于挖掘流水线的评测任务）
    # ========================================================
    def start_benchmark(self, opts):
        with self._lock:
            if self.state in ("running", "waiting"):
                return False, "已有任务在运行中"
            self.bus.reset()
            self.stop_flag.clear()
            self.state = "running"
            self.task_kind = "skill_benchmark"
            self.last_result = None
            # reset 事件：通知客户端清空 benchmark 相关面板（保留流水线结果展示）
            self.emit("reset", scope="benchmark")
            self.thread = threading.Thread(
                target=self._run_benchmark, args=(opts,), daemon=True)
            self.thread.start()
            return True, "started"

    def start_trajectory_benchmark(self, body):
        """Start an isolated trajectory -> Benchmark job.

        Request normalization happens before the worker is launched so malformed
        trajectories receive an immediate HTTP 400 instead of an asynchronous
        error.  The normalized evidence is passed only to the worker; snapshots
        expose source counts/digests, never the full trajectory text.
        """
        request = tb.normalize_request(body)
        public_config = {
            "run_id": request["run_id"],
            "dataset_name": request["dataset_name"],
            "target_total": request["target_total"],
            "difficulty_dist": request["difficulty_dist"],
            **request["source"],
        }
        with self._lock:
            if self.state in ("running", "waiting"):
                return False, "已有任务在运行中", None
            self.bus.reset()
            self.stop_flag.clear()
            self.state = "running"
            self.task_kind = "trajectory_benchmark"
            self.last_result = None
            self.config = public_config
            self.emit("reset", scope="trajectory_benchmark")
            self.emit(
                "trajectory_benchmark_status",
                state="running",
                **public_config,
            )
            self.thread = threading.Thread(
                target=self._run_trajectory_benchmark,
                args=(request,),
                daemon=True,
            )
            self.thread.start()
            return True, "started", public_config

    def _run_trajectory_benchmark(self, request):
        try:
            result = tb.mine_trajectory_benchmark(
                request,
                project_root=PROJECT_ROOT,
                stop_requested=self.stop_flag.is_set,
                log=self.log,
            )
            summary = {
                key: result.get(key)
                for key in (
                    "run_id",
                    "dataset_name",
                    "dataset_format",
                    "origin",
                    "state",
                    "question_count",
                    "target_total",
                    "difficulty_counts",
                    "dimensions",
                    "trajectory_count",
                    "evidence_turn_count",
                    "source_sha256",
                    "artifact_dir",
                )
            }
            self.last_result = summary
            self.state = "done"
            self.emit("trajectory_benchmark_status", **summary)
            self.emit("trajectory_benchmark_done", **summary)
        except tb.TrajectoryBenchmarkStopped as exc:
            summary = {
                "run_id": request["run_id"],
                "dataset_name": request["dataset_name"],
                "state": "stopped",
                "reason": str(exc),
            }
            self.last_result = summary
            self.state = "done"
            self.emit("trajectory_benchmark_status", **summary)
            self.emit("trajectory_benchmark_done", **summary)
        except Exception as exc:
            summary = {
                "run_id": request["run_id"],
                "dataset_name": request["dataset_name"],
                "state": "error",
                "reason": str(exc),
            }
            self.last_result = summary
            self.state = "error"
            self.log(f"轨迹 Benchmark 挖掘失败：{exc}", level="error")
            self.emit("trajectory_benchmark_status", **summary)
            self.emit("trajectory_benchmark_error", **summary)

    def _run_benchmark(self, opts):
        bus_writer = _BusWriter(self)
        try:
            self.emit("bench_status", state="running")
            self.log("===== Benchmark 任务开始 =====")
            dist_spec = (opts.get("difficulty_dist") or "").strip()
            target_total = int(opts.get("target_total") or rb.DEFAULT_TARGET_TOTAL)
            skip_build = bool(opts.get("skip_build", False))
            limit = opts.get("limit")
            limit = int(limit) if limit else None
            mode = opts.get("mode", "dialogue")

            difficulty_counts = None
            if dist_spec:
                dist = rb.parse_difficulty_dist(dist_spec)
                difficulty_counts = rb.dist_to_counts(dist, max(1, target_total))
                self.log(f"[难度分布] 目标配额：{difficulty_counts}（来自 '{dist_spec}' × {target_total}）")

            with contextlib.redirect_stdout(bus_writer):
                if not rp.check_hermes_installed():
                    self.emit("bench_error", msg="未找到 hermes 可执行文件")
                    self.state = "error"
                    return
                skill_dir = _find_compiled_skill(opts.get("skill_name", ""))
                if not skill_dir:
                    requested = str(opts.get("skill_name") or "").strip()
                    msg = (f"未找到已编译 Skill：{requested}" if requested
                           else "compiled_skill/ 下没有找到含 SKILL.md 的 skill")
                    self.emit("bench_error", msg=msg)
                    self.state = "error"
                    return
                skill_md = skill_dir / "SKILL.md"
                skill_name = rst.parse_skill_name(skill_md)
                rp.ensure_hermes_home()
                hermes_env, has_key = rp.build_hermes_env()
                self.log("✓ 已解析 ARK_API_KEY" if has_key else "⚠️ 未找到 ARK_API_KEY")
                if not rp.test_model_connection(hermes_env):
                    self.emit("bench_error", msg="模型连接测试失败，请检查凭据、额度、网络和 Hermes 模型配置")
                    self.state = "error"
                    return

            if self.stop_flag.is_set():
                self.emit("bench_done", stopped=True)
                self.state = "done"
                return

            # 阶段一：构建 / 载入题库
            self.emit("bench_phase", phase="build", state="active")
            with contextlib.redirect_stdout(bus_writer):
                if skip_build:
                    questions = rb.load_existing_benchmark(skill_dir)
                else:
                    questions = rb.build_phase(skill_dir, skill_name, hermes_env,
                                               difficulty_counts=difficulty_counts)
            if not questions:
                self.emit("bench_error", msg="题库为空（构建失败或未找到 benchmark.jsonl）")
                self.state = "error"
                return
            self.emit("bench_phase", phase="build", state="done", n=len(questions))
            self.log(f"题库就绪：{len(questions)} 道题")

            if bool(opts.get("build_only", False)):
                self.log(f"✓ Benchmark 题库已生成：{skill_dir.name} · {len(questions)} 道题")
                self.emit("bench_done", stopped=False, build_only=True,
                          skill=skill_dir.name, question_count=len(questions))
                self.state = "done"
                return

            if self.stop_flag.is_set():
                self.emit("bench_done", stopped=True)
                self.state = "done"
                return

            # 阶段二：逐题跑分（每题之间检查中止请求）
            self.emit("bench_phase", phase="run", state="active")
            with contextlib.redirect_stdout(bus_writer):
                rst.deploy_test_skill(skill_dir, skill_name)
                _, eval_text, _ = rb.load_skill_and_eval(skill_dir)
                results = rb.run_phase(skill_name, questions, eval_text, hermes_env,
                                       limit=limit, mode=mode,
                                       max_turns=int(opts.get("max_turns") or rb.DEFAULT_MAX_TURNS),
                                       should_stop=self.stop_flag.is_set)
            self.emit("bench_phase", phase="run", state="done")

            if self.stop_flag.is_set():
                self.log(f"■ 已中止：完成 {len(results)}/{len(questions)} 题，跳过聚合",
                         level="warn")
                self.emit("bench_done", stopped=True)
                self.state = "done"
                return

            # 阶段三：聚合 + 报告
            self.emit("bench_phase", phase="aggregate", state="active")
            with contextlib.redirect_stdout(bus_writer):
                agg = rb.aggregate(results, difficulty_target=difficulty_counts)
                rb.write_report(agg, results, skill_name)
            self.emit("bench_phase", phase="aggregate", state="done")

            self.emit("bench_result", **_benchmark_payload(agg, skill_name))
            self.log(f"✓ Benchmark 完成：得分 {agg['bench_score']} · 通过率 {agg['pass_rate']}%")
            self.emit("bench_done", stopped=False)
            self.state = "done"
        except Exception as e:
            self.emit("bench_error", msg=f"Benchmark 运行异常：{e}")
            self.state = "error"
            self.log(f"Benchmark 运行异常：{e}", level="error")


class _BusWriter:
    """把 print 的行转发成 SSE log 事件。用于真实模式捕获 run_pipeline 的输出。"""
    def __init__(self, mgr):
        self.mgr = mgr
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = rp._filter_noise(line) if hasattr(rp, "_filter_noise") else line
            if line.strip():
                self.mgr.log(line.rstrip())

    def flush(self):
        if self._buf.strip():
            self.mgr.log(self._buf.rstrip())
            self._buf = ""


MANAGER = RunManager()
LIFT_MANAGER = li.LiftRunManager(MANAGER.emit)


# ============================================================
# Benchmark / 覆盖报告：把聚合结果整理成前端友好的 payload
# ============================================================
def _benchmark_payload(agg, skill_name):
    """把 aggregate() 的结果整理成前端表格数据（含难度分布对比）。"""
    levels = rb.DIFFICULTY_LEVELS
    da = agg.get("difficulty_actual", {})
    dap = agg.get("difficulty_actual_pct", {})
    dt = agg.get("difficulty_target")
    dsr = agg.get("difficulty_score_rate", {})
    diff_rows = []
    for lv in levels:
        row = {
            "level": lv,
            "actual": da.get(lv, 0),
            "actual_pct": dap.get(lv, 0.0),
            "score_rate": dsr.get(lv),
        }
        if dt:
            row["target"] = dt[lv]["count"]
            row["target_pct"] = dt[lv]["pct"]
        diff_rows.append(row)
    return {
        "skill": skill_name,
        "bench_score": agg["bench_score"],
        "pass_rate": agg["pass_rate"],
        "n": agg["n"],
        "overall_counts": agg["overall_counts"],
        "safety_pass": agg["safety_pass"],
        "safety_known": agg["safety_known"],
        "has_target": bool(dt),
        "difficulty_rows": diff_rows,
        "dialogue_n": agg.get("dialogue_n", 0),
        "ended_ok": agg.get("ended_ok", 0),
        "avg_turns": agg.get("avg_turns"),
    }


def build_coverage_payload(skill_name=None):
    """调 run_coverage_report 解析并整理成前端表格数据。返回 (ok, payload_or_msg)。"""
    reports = rc.load_semantic_reports()
    if not reports:
        return False, "未找到语义报告 semantic_reports/*.md"
    skill_md = rc.find_skill_md(skill_name)
    if not skill_md:
        return False, "未找到 compiled_skill/<skill>/SKILL.md"
    sref = rc.parse_skill_references(skill_md)
    cov = rc.compute_coverage(reports, sref)
    # 落地报告文件（与命令行一致）
    try:
        rc.write_reports(cov, reports, skill_md, skill_md.parent.name)
    except Exception:
        pass
    uc = cov["unit_coverage"]
    gc = cov["gap_coverage"]
    return True, {
        "skill": skill_md.parent.name,
        "reports": [r["path"].name for r in reports],
        "unit": {
            "total": uc["total_units"], "adopted": uc["adopted"],
            "dropped": uc["dropped"], "adopt_rate": uc["adopt_rate"],
            "by_retention": uc["by_retention"],
            "dropped_units": uc["dropped_units"],
        },
        "gap": {
            "total": gc["total_gaps"], "resolved": gc["resolved"],
            "unresolved": gc["unresolved"], "resolve_rate": gc["resolve_rate"],
            "by_severity": gc["by_severity"],
        },
        "dim": cov["dim_coverage"],
    }


# ============================================================
# 配置项：暴露给前端的可配置面
# ============================================================
def _model_config_path():
    return PROJECT_ROOT / ".hermes_home" / "config.yaml"


def _load_model_config_document():
    """Load the project-owned model runtime config without exposing secrets."""
    path = _model_config_path()
    fallback = PROJECT_ROOT / "hermes" / "config.yaml.example"
    source = path if path.is_file() else fallback
    if not source.is_file():
        return {}
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8", errors="ignore")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"模型配置文件格式错误：{exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("模型配置文件必须是 YAML 对象")
    return loaded


def get_mining_model_settings(document=None):
    """Return the public, secret-free settings used by new mining tasks."""
    config_document = document if isinstance(document, dict) else _load_model_config_document()
    model_config = config_document.get("model") or {}
    if not isinstance(model_config, dict):
        model_config = {}
    model_id = str(model_config.get("default") or model_config.get("model") or "").strip()
    base_url = str(model_config.get("base_url") or "").strip()
    try:
        max_tokens = int(model_config.get("max_tokens") or 32768)
    except (TypeError, ValueError):
        max_tokens = 32768
    try:
        temperature = float(model_config.get("temperature") if model_config.get("temperature") is not None else 0.2)
    except (TypeError, ValueError):
        temperature = 0.2
    inline_key = str(model_config.get("api_key") or model_config.get("api") or "").strip()
    inherited_key = ""
    if not inline_key:
        try:
            inherited_key = rp.resolve_ark_key()
        except Exception:
            inherited_key = ""
    return {
        "provider": "openai-compatible",
        "id": model_id,
        "model": model_id,
        "base_url": base_url,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "api_key_present": bool(inline_key or inherited_key),
        "configured": bool(model_id and base_url),
    }


def _normalize_mining_model_payload(body, document=None):
    if not isinstance(body, dict):
        raise ValueError("模型配置必须是对象")
    config_document = document if isinstance(document, dict) else _load_model_config_document()
    current = config_document.get("model") or {}
    if not isinstance(current, dict):
        current = {}

    model_id = str(body.get("model") or body.get("id") or current.get("default") or "").strip()
    if not model_id:
        raise ValueError("请填写模型名称")
    if len(model_id) > 200 or any(ch in model_id for ch in "\r\n"):
        raise ValueError("模型名称格式不正确")

    base_url = str(body.get("base_url") or current.get("base_url") or "").strip().rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP(S) 地址")

    try:
        max_tokens = int(body.get("max_tokens") or current.get("max_tokens") or 32768)
    except (TypeError, ValueError) as exc:
        raise ValueError("最大输出 Token 必须是整数") from exc
    if not 1 <= max_tokens <= 131072:
        raise ValueError("最大输出 Token 必须在 1 到 131072 之间")

    raw_temperature = body.get("temperature")
    if raw_temperature is None:
        raw_temperature = current.get("temperature", 0.2)
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError) as exc:
        raise ValueError("Temperature 必须是数字") from exc
    if not 0 <= temperature <= 2:
        raise ValueError("Temperature 必须在 0 到 2 之间")

    incoming_key = str(body.get("api_key") or "").strip()
    if any(ch in incoming_key for ch in "\r\n") or len(incoming_key) > 4096:
        raise ValueError("API Key 格式不正确")
    clear_key = bool(body.get("clear_api_key", False))
    current_key = str(current.get("api_key") or current.get("api") or "").strip()
    if clear_key:
        effective_key = ""
    elif incoming_key:
        effective_key = incoming_key
    elif current_key:
        effective_key = current_key
    else:
        effective_key = rp.resolve_ark_key()

    return {
        "model": model_id,
        "base_url": base_url,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "api_key": incoming_key,
        "clear_api_key": clear_key,
    }, effective_key


def save_mining_model_settings(body):
    """Persist the editable mining model settings for subsequent jobs."""
    with MODEL_CONFIG_LOCK:
        document = _load_model_config_document()
        normalized, _ = _normalize_mining_model_payload(body, document)
        model_config = document.get("model") or {}
        if not isinstance(model_config, dict):
            model_config = {}
        model_config.update({
            "default": normalized["model"],
            "provider": "custom",
            "base_url": normalized["base_url"],
            "api_mode": "chat_completions",
            "max_tokens": normalized["max_tokens"],
            "temperature": normalized["temperature"],
        })
        model_config.pop("model", None)
        if normalized["clear_api_key"]:
            model_config.pop("api_key", None)
            model_config.pop("api", None)
        elif normalized["api_key"]:
            model_config["api_key"] = normalized["api_key"]
            model_config.pop("api", None)
        document["model"] = model_config
        document = rp.hi.sanitize_config_document(document)

        path = _model_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return get_mining_model_settings(document)


def test_mining_model_settings(body):
    """Probe the OpenAI-compatible endpoint using edited or saved settings."""
    document = _load_model_config_document()
    normalized, api_key = _normalize_mining_model_payload(body, document)
    if not api_key:
        raise ValueError("请先填写并保存 API Key")
    endpoint = normalized["base_url"]
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    payload = json.dumps({
        "model": normalized["model"],
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 8,
        "temperature": 0,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        detail = detail.replace(api_key, "***")
        raise ValueError(f"模型接口返回 HTTP {exc.code}：{detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = str(exc).replace(api_key, "***")
        raise ValueError(f"无法连接模型接口：{message[:300]}") from exc
    try:
        response_payload = json.loads(raw)
        response_text = str(response_payload["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型接口返回格式不符合 OpenAI Chat Completions 规范") from exc
    return {
        "ok": True,
        "model": normalized["model"],
        "latency_ms": round((time.monotonic() - started) * 1000),
        "response": response_text[:200],
    }


def build_config_schema():
    # 探测可选输入目录
    data_dir = PROJECT_ROOT / "data"
    input_sources = []
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if d.is_dir():
                input_sources.append(_input_source_detail(d))
    if not input_sources:
        input_sources.append(_input_source_detail(data_dir / "input"))
    input_dirs = [source["path"] for source in input_sources]
    default_input_dir = "data/input" if "data/input" in input_dirs else input_dirs[0]
    # 探测已编译 skill
    compiled_details = [
        {"name": item["name"], "has_skill": True, **item}
        for item in _compiled_skill_details()
    ]
    compiled = [item["name"] for item in compiled_details]
    model_settings = get_mining_model_settings()
    return {
        "input_dirs": input_dirs,
        "input_sources": input_sources,
        "default_input_dir": default_input_dir,
        "max_rounds_default": 3,
        "max_rounds_range": [1, 5],
        "model": model_settings,
        "compiled_skills": compiled,
        "compiled_skill_details": compiled_details,
        "benchmark": {
            "difficulty_levels": rb.DIFFICULTY_LEVELS,
            "default_dist": "easy:3,medium:8,hard:7",
            "default_total": rb.DEFAULT_TARGET_TOTAL,
            "modes": [
                {"value": "dialogue", "label": "多轮对话（模拟参与者 ↔ skill）"},
                {"value": "single", "label": "单轮作答"},
            ],
        },
        "trajectory_benchmark": {
            "endpoint": "/api/trajectory-benchmarks",
            "unified_endpoint": "/api/mining/trajectory-benchmarks",
            "dataset_format": tb.DATASET_FORMAT,
            "default_target_total": tb.DEFAULT_TARGET_TOTAL,
            "max_target_total": tb.MAX_TARGET_TOTAL,
            "max_trajectories": tb.MAX_TRAJECTORIES,
        },
        "checkpoints": [
            {"key": "after_semantic", "label": "Skill 生成前关键知识补证",
             "desc": "Step2 发现缺失的业务规则、准确阈值或例外后，在编译 Skill 前逐项补全"},
            {"key": "after_compile", "label": "编译 skill 后校验",
             "desc": "每轮产出 SKILL.md 后，请你人工校验关键条目"},
            {"key": "on_gap_low_confidence", "label": "发现缺口 / 置信度低时",
             "desc": "缺口多或置信档偏低时，停下来请你补充领域知识"},
            {"key": "before_reflection", "label": "每轮反思前",
             "desc": "进入下一轮反思补跑前，请你确认是否继续 / 追加素材"},
        ],
    }


# ============================================================
# HTTP 处理
# ============================================================
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 静默默认访问日志

    # --- 工具 ---
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes=1_000_000):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        if length > max_bytes:
            raise ValueError(f"请求体超过 {max_bytes // 1024 // 1024 or 1} MB 上限")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError("请求体不是有效 JSON") from e
        if not isinstance(body, dict):
            raise ValueError("JSON 请求体必须是对象")
        return body

    def _serve_static(self, rel_path):
        # 默认 index.html
        if rel_path in ("", "/"):
            rel_path = "index.html"
        static_root = STATIC_DIR.resolve()
        target = (static_root / rel_path).resolve()
        if (target != static_root and static_root not in target.parents) or not target.exists():
            self.send_error(404, "Not Found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET ---
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            self._send_json(build_config_schema())
        elif path == "/api/model":
            try:
                self._send_json(get_mining_model_settings())
            except ValueError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=400)
        elif path == "/api/state":
            self._send_json(MANAGER.snapshot())
        elif path == "/api/runs":
            self._send_json({"runs": list_mining_runs(), "runner": MANAGER.snapshot()})
        elif path == "/api/jobs":
            self._send_json({"jobs": list_all_mining_jobs(), "summary": JOBS.summary()})
        elif path == "/api/sources/status":
            query = parse_qs(parsed.query)
            try:
                source_dir = _knowledge_source_dir((query.get("source_path") or ["data/input"])[0])
                self._send_json({"ok": True, "source": _input_source_detail(source_dir)})
            except ValueError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=400)
        elif path.startswith("/api/jobs/"):
            job_id = unquote(path.removeprefix("/api/jobs/").strip("/"))
            try:
                self._send_json({"ok": True, "job": get_mining_job(job_id)})
            except KeyError:
                self._send_json({"ok": False, "msg": "挖掘任务不存在"}, code=404)
        elif path == "/api/artifacts/content":
            query = parse_qs(parsed.query)
            try:
                self._send_json(read_history_artifact((query.get("path") or [""])[0]))
            except FileNotFoundError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=404)
            except ValueError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=400)
        elif path == "/api/trajectory-benchmarks":
            self._send_json({
                "runs": tb.list_runs(project_root=PROJECT_ROOT),
                "runner": MANAGER.snapshot(),
                "dataset_format": tb.DATASET_FORMAT,
            })
        elif path.startswith("/api/trajectory-benchmarks/"):
            run_id = path.removeprefix("/api/trajectory-benchmarks/").strip("/")
            try:
                result = tb.get_run(run_id, project_root=PROJECT_ROOT)
                if result is None:
                    snapshot = MANAGER.snapshot()
                    active_config = snapshot.get("config") or {}
                    if (
                        snapshot.get("task_kind") == "trajectory_benchmark"
                        and active_config.get("run_id") == run_id
                    ):
                        last_result = snapshot.get("last_result") or {}
                        self._send_json({
                            "ok": True,
                            "run_id": run_id,
                            "state": last_result.get("state") or snapshot.get("state"),
                            "dataset_format": tb.DATASET_FORMAT,
                            "run": active_config,
                            "result": last_result or None,
                            "questions": [],
                        })
                    else:
                        self._send_json({"ok": False, "msg": "未找到轨迹 Benchmark"}, code=404)
                else:
                    self._send_json({"ok": True, **result})
            except tb.TrajectoryBenchmarkError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=400)
        elif path == "/api/lift/status":
            self._send_json({
                "integration": li.lift_status(),
                "source_skills": li.list_source_skills(),
                "drafts": li.list_drafts(),
                "runner": LIFT_MANAGER.snapshot(),
            })
        elif path.startswith("/api/lift/drafts/"):
            draft_id = path.removeprefix("/api/lift/drafts/").strip("/")
            try:
                self._send_json(li.get_draft(draft_id))
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except Exception as e:
                self._send_json({"ok": False, "msg": f"读取 LIFT 草稿失败：{e}"}, code=400)
        elif path == "/api/events":
            self._serve_sse()
        elif path.startswith("/api/"):
            self.send_error(404, "Unknown API")
        else:
            self._serve_static(path.lstrip("/"))

    # --- POST ---
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = {}
        if path != "/api/stop":
            try:
                if path == "/api/sources/upload":
                    max_bytes = MAX_KNOWLEDGE_REQUEST_BYTES
                elif path in {"/api/trajectory-benchmarks", "/api/benchmark/from-trajectories"}:
                    max_bytes = MAX_TRAJECTORY_BENCHMARK_REQUEST_BYTES
                elif path == "/api/artifacts/content":
                    max_bytes = MAX_EDITABLE_ARTIFACT_BYTES + 64 * 1024
                else:
                    max_bytes = 1_000_000
                body = self._read_json_body(max_bytes=max_bytes)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
                return
        if path == "/api/artifacts/content":
            try:
                self._send_json(save_history_artifact(body.get("path"), body.get("content")))
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"保存挖掘产物失败：{e}"}, code=500)
        elif path == "/api/sources/upload":
            if MANAGER.state in ("running", "waiting"):
                self._send_json(
                    {"ok": False, "msg": "挖掘任务运行中，暂不能修改输入文档"},
                    code=409,
                )
                return
            try:
                self._send_json(save_uploaded_knowledge(body), code=201)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"写入知识源失败：{e}"}, code=500)
        elif path == "/api/sources":
            try:
                self._send_json(create_knowledge_source(body), code=201)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"创建知识源失败：{e}"}, code=500)
        elif path == "/api/sources/merge":
            try:
                self._send_json(merge_knowledge_sources(body), code=201)
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"合并知识源失败：{e}"}, code=500)
        elif path.startswith("/api/sources/") and path.endswith("/rename"):
            source_name = unquote(
                path.removeprefix("/api/sources/").removesuffix("/rename").strip("/")
            )
            try:
                self._send_json(rename_knowledge_source(source_name, body))
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except FileExistsError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=409)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"重命名知识源失败：{e}"}, code=500)
        elif path == "/api/jobs":
            try:
                jobs = JOBS.create_jobs(body)
                self._send_json({"ok": True, "jobs": jobs, "summary": JOBS.summary()}, code=201)
            except mj.MiningJobError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"创建挖掘任务失败：{e}"}, code=500)
        elif path == "/api/model":
            try:
                self._send_json(save_mining_model_settings(body))
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"保存模型配置失败：{e}"}, code=500)
        elif path == "/api/model/test":
            try:
                self._send_json(test_mining_model_settings(body))
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
        elif path.startswith("/api/jobs/") and path.endswith("/stop"):
            job_id = unquote(path.removeprefix("/api/jobs/").removesuffix("/stop").strip("/"))
            try:
                self._send_json({"ok": True, "job": JOBS.stop_job(job_id)})
            except KeyError:
                self._send_json({"ok": False, "msg": "挖掘任务不存在"}, code=404)
            except mj.MiningJobError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=409)
        elif path.startswith("/api/jobs/") and path.endswith("/answer"):
            job_id = unquote(path.removeprefix("/api/jobs/").removesuffix("/answer").strip("/"))
            try:
                self._send_json({
                    "ok": True,
                    "job": JOBS.submit_checkpoint_answer(job_id, body),
                })
            except KeyError:
                self._send_json({"ok": False, "msg": "挖掘任务不存在"}, code=404)
            except mj.MiningJobError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=409)
        elif path == "/api/run":
            if LIFT_MANAGER.state in ("running", "stopping"):
                self._send_json({"ok": False, "msg": "LIFT 任务运行中，暂不能启动挖掘"}, code=409)
                return
            cfg = _normalize_config(body)
            ok, msg = MANAGER.start(cfg)
            self._send_json({"ok": ok, "msg": msg, "config": cfg}, code=200 if ok else 409)
        elif path == "/api/answer":
            ok, msg = MANAGER.submit_answer({
                # question_id 必须透传：submit_answer 用它校验答案对应「当前」提问，
                # ask_questions 的阻塞循环也靠它匹配放行。丢了它会导致答案被判过期、
                # worker 线程永久卡在 answer_q.get() 上（检查点死锁）。
                "question_id": body.get("question_id"),
                "answers": body.get("answers", {}) or {},
                "stop": bool(body.get("stop", False)),
            })
            self._send_json({"ok": ok, "msg": msg}, code=200 if ok else 409)
        elif path == "/api/stop":
            ok, msg = MANAGER.request_stop()
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/benchmark":
            if LIFT_MANAGER.state in ("running", "stopping"):
                self._send_json({"ok": False, "msg": "LIFT 任务运行中，暂不能启动 Benchmark"}, code=409)
                return
            ok, msg = MANAGER.start_benchmark({
                "skill_name": str(body.get("skill_name") or "").strip(),
                "difficulty_dist": body.get("difficulty_dist", ""),
                "target_total": body.get("target_total", rb.DEFAULT_TARGET_TOTAL),
                "skip_build": bool(body.get("skip_build", False)),
                "build_only": bool(body.get("build_only", False)),
                "limit": body.get("limit"),
                "mode": body.get("mode", "dialogue"),
                "max_turns": body.get("max_turns", rb.DEFAULT_MAX_TURNS),
            })
            self._send_json({"ok": ok, "msg": msg}, code=200 if ok else 409)
        elif path in {"/api/trajectory-benchmarks", "/api/benchmark/from-trajectories"}:
            try:
                ok, msg, run = MANAGER.start_trajectory_benchmark(body)
                self._send_json(
                    {
                        "ok": ok,
                        "msg": msg,
                        "run": run,
                        "dataset_format": tb.DATASET_FORMAT,
                        "status_path": f"/api/trajectory-benchmarks/{run['run_id']}" if run else None,
                        "unified_status_path": (
                            f"/api/mining/trajectory-benchmarks/{run['run_id']}" if run else None
                        ),
                    },
                    code=202 if ok else 409,
                )
            except tb.TrajectoryBenchmarkError as exc:
                self._send_json({"ok": False, "msg": str(exc)}, code=400)
        elif path == "/api/coverage":
            try:
                ok, payload = build_coverage_payload(body.get("skill") or None)
            except Exception as e:
                ok, payload = False, f"覆盖报告生成异常：{e}"
            if ok:
                self._send_json({"ok": True, "coverage": payload})
            else:
                self._send_json({"ok": False, "msg": payload}, code=400)
        elif path == "/api/lift/drafts":
            try:
                result = li.create_draft(
                    str(body.get("skill_name") or ""),
                    suite_name=str(body.get("suite_name") or "").strip() or None,
                    category=str(body.get("category") or "").strip() or None,
                    warmup_ratio=float(body.get("warmup_ratio") or 2 / 3),
                    origin="web-review",
                )
                self._send_json({"ok": True, **result}, code=201)
            except Exception as e:
                self._send_json({"ok": False, "msg": f"生成 LIFT 草稿失败：{e}"}, code=400)
        elif path.startswith("/api/lift/drafts/"):
            tail = path.removeprefix("/api/lift/drafts/").strip("/")
            parts = tail.split("/") if tail else []
            if not parts:
                self._send_json({"ok": False, "msg": "缺少 draft id"}, code=400)
                return
            draft_id = parts[0]
            action = parts[1] if len(parts) > 1 else "save"
            try:
                if action == "save":
                    result = li.save_draft(draft_id, body.get("suite"))
                elif action == "approve":
                    reviewer = str(
                        self.headers.get("X-teamEvolver-Reviewer")
                        or body.get("reviewer")
                        or "human-reviewer"
                    )
                    result = li.approve_draft(
                        draft_id,
                        reviewer,
                        str(body.get("note") or ""),
                    )
                elif action == "publish":
                    result = li.publish_draft(draft_id)
                else:
                    self._send_json({"ok": False, "msg": f"未知草稿操作：{action}"}, code=404)
                    return
                self._send_json({"ok": True, **result})
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except Exception as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
        elif path == "/api/lift/run":
            if MANAGER.state in ("running", "waiting"):
                self._send_json(
                    {"ok": False, "msg": "挖掘或 Benchmark 任务运行中，暂不能启动 LIFT"},
                    code=409,
                )
                return
            try:
                ok, msg, run = LIFT_MANAGER.start(body)
                self._send_json({"ok": ok, "msg": msg, "run": run}, code=200 if ok else 409)
            except Exception as e:
                self._send_json({"ok": False, "msg": f"启动 LIFT 失败：{e}"}, code=400)
        elif path == "/api/lift/stop":
            ok, msg = LIFT_MANAGER.stop()
            self._send_json({"ok": ok, "msg": msg}, code=200 if ok else 409)
        else:
            self.send_error(404, "Unknown API")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/sources/"):
            source_name = unquote(path.removeprefix("/api/sources/").strip("/"))
            try:
                self._send_json(delete_knowledge_source(source_name))
            except FileNotFoundError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=404)
            except ValueError as e:
                self._send_json({"ok": False, "msg": str(e)}, code=400)
            except OSError as e:
                self._send_json({"ok": False, "msg": f"删除知识源失败：{e}"}, code=500)
        else:
            self.send_error(404, "Unknown API")

    # --- SSE ---
    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # Last-Event-ID 断点续传：浏览器自动重连时带上最后收到的事件 id，
        # 只补发之后的事件，不再全量重放历史（根治重连后日志/卡片翻倍）
        raw_last_id = str(self.headers.get("Last-Event-ID", "") or "")
        try:
            if ":" in raw_last_id:
                prior_stream, prior_seq = raw_last_id.rsplit(":", 1)
                last_seq = int(prior_seq) if prior_stream == MANAGER.bus.stream_id else 0
            else:
                last_seq = int(raw_last_id or 0)  # 兼容升级前仅含数字的事件 id
        except (TypeError, ValueError):
            last_seq = 0
        q = MANAGER.bus.subscribe(after_seq=last_seq)
        try:
            # 先推一次当前状态（合成事件，不带 id，不影响续传游标）
            self._sse_write({"type": "status", "state": MANAGER.state,
                             "stream_id": MANAGER.bus.stream_id})
            while True:
                try:
                    ev = q.get(timeout=15)
                    self._sse_write(ev)
                except queue.Empty:
                    # 心跳，保持连接
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            MANAGER.bus.unsubscribe(q)

    def _sse_write(self, ev):
        data = json.dumps(ev, ensure_ascii=False)
        # 带 seq 的事件写入 SSE id 字段，供浏览器重连时回传 Last-Event-ID
        prefix = f"id: {ev.get('stream_id', MANAGER.bus.stream_id)}:{ev['seq']}\n" if ev.get("seq") else ""
        self.wfile.write(f"{prefix}data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


def _normalize_config(body):
    checkpoints_in = body.get("checkpoints", {}) or {}
    if not isinstance(checkpoints_in, dict):
        checkpoints_in = {}
    input_dir = body.get("input_dir", "data/input")
    if not isinstance(input_dir, str) or not input_dir.strip():
        input_dir = "data/input"
    model = body.get("model", {})
    if not isinstance(model, dict):
        model = {}
    try:
        max_rounds = int(body.get("max_rounds", 3) or 3)
    except (TypeError, ValueError):
        max_rounds = 3
    return {
        "input_dir": input_dir.strip(),
        "max_rounds": max(1, min(5, max_rounds)),
        "ask_enabled": bool(body.get("ask_enabled", True)),
        "checkpoints": {
            "after_semantic": bool(checkpoints_in.get("after_semantic", True)),
            "after_compile": bool(checkpoints_in.get("after_compile", True)),
            "on_gap_low_confidence": bool(checkpoints_in.get("on_gap_low_confidence", True)),
            "before_reflection": bool(checkpoints_in.get("before_reflection", True)),
        },
        "model": model,
    }


def main():
    port = int(os.environ.get("PORT", "8765"))
    ki.mark_interrupted_ingestions(PROJECT_ROOT)
    JOBS.start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    prior_sigterm = signal.getsignal(signal.SIGTERM)

    def request_shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    print(f"✓ 控制台已启动： http://127.0.0.1:{port}")
    print(f"  项目根： {PROJECT_ROOT}")
    print("  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()
    finally:
        JOBS.shutdown()
        server.server_close()
        signal.signal(signal.SIGTERM, prior_sigterm)


if __name__ == "__main__":
    main()
