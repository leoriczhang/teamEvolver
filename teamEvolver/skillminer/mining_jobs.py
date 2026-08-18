"""Persistent, isolated and parallel SkillMiner job orchestration.

Each job snapshots one knowledge source into its own workspace and starts the
existing pipeline in a separate process.  That process boundary is important:
``run_pipeline`` still uses module globals for paths and active Hermes child
processes, so threads in one interpreter cannot safely run it concurrently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import benchmark_format as bf
import human_checkpoints as hc
import knowledge_ingestion as ki

_STATIC_WORKSPACE_ITEMS = (
    "sample-package-constructor-agent-skill",
    "semantic-discovery-agent-skill",
    "evaluation-compiler-agent-skill",
    "sample_package_constructor_agent_prompt.py",
    "semantic_discovery_agent_prompt.py",
    "evaluation_compiler_agent_prompt.py",
)
_ARTIFACT_NAMES = {
    "SKILL.md": "skill",
    "EVALUATION.md": "evaluation",
    "BENCHMARK.md": "benchmark",
    "benchmark.json": "benchmark",
    "benchmark_bank.json": "benchmark",
    "benchmark_quality.json": "benchmark",
}
_ACTIVE_STATES = {"preparing", "queued", "running", "waiting", "stopping"}
_PHASE_RE = re.compile(r"\[第\s*(\d+)\s*轮\]\[Step\s*([123])/3\]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: Any, fallback: str = "mining") -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw)
    return "-".join(part for part in slug.split("-") if part)[:48] or fallback


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _visible_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


class MiningJobError(ValueError):
    pass


class MiningJobManager:
    """Owns the persistent job registry and a bounded process pool."""

    def __init__(
        self,
        project_root: Path | str,
        *,
        max_parallel: int | None = None,
        python_executable: str | None = None,
        start_immediately: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.jobs_root = self.project_root / "mining_jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        requested = max_parallel or int(os.environ.get("SKILLMINER_MAX_PARALLEL_JOBS", "3") or 3)
        self.max_parallel = max(1, min(8, int(requested)))
        self.python_executable = python_executable or sys.executable
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._shutting_down = False
        self._load_existing()
        if start_immediately:
            self.start()

    def _load_existing(self) -> None:
        for meta_path in sorted(self.jobs_root.glob("*/job.json")):
            try:
                job = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(job.get("job_id") or meta_path.parent.name)
            job["job_id"] = job_id
            self._jobs[job_id] = job

    def start(self) -> None:
        """Recover orphaned jobs and start queued work when the service starts.

        Loading the module is intentionally read-only.  Test discovery and CLI
        utilities import this class too, and must not mutate jobs owned by an
        already-running SkillMiner service.
        """
        with self._lock:
            self._shutting_down = False
            self._recover_interrupted_jobs()
        self._dispatch()

    def _recover_interrupted_jobs(self) -> None:
        for job in self._jobs.values():
            if job.get("status") in {"preparing", "running", "waiting", "stopping"}:
                job["status"] = "interrupted"
                job["finished_at"] = _utc_now()
                job["error"] = "服务重启导致任务中断，可重新创建任务继续挖掘"
                checkpoint_dir = self._job_dir(job["job_id"]) / "checkpoints"
                (checkpoint_dir / "pending.json").unlink(missing_ok=True)
                (checkpoint_dir / "answer.json").unlink(missing_ok=True)
                self._persist(job)

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def _persist(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _utc_now()
        _write_json_atomic(self._job_dir(job["job_id"]) / "job.json", job)

    def _source_dir(self, source_path: str) -> Path:
        raw = str(source_path or "").strip().replace("\\", "/").rstrip("/")
        parts = raw.split("/")
        if len(parts) != 2 or parts[0] != "data" or not parts[1] or parts[1].startswith("."):
            raise MiningJobError("知识源必须是 data/ 下的一级目录")
        data_root = (self.project_root / "data").resolve()
        target = (data_root / parts[1]).resolve()
        if target.parent != data_root or not target.is_dir():
            raise MiningJobError(f"知识源不存在：{raw}")
        files = _visible_files(target)
        if not files:
            raise MiningJobError(f"知识源中没有可挖掘文档：{raw}")
        ingestion = ki.read_ingestion_state(
            self.project_root,
            target.name,
            has_documents=True,
        )
        if ingestion["status"] == "processing":
            raise MiningJobError(f"知识源正在后处理，完成前不能用于挖掘：{raw}")
        if ingestion["status"] == "failed":
            detail = str(ingestion.get("error") or "请重新上传文件")
            raise MiningJobError(f"知识源后处理失败，不能用于挖掘：{detail}")
        if ingestion["status"] != "ready":
            raise MiningJobError(f"知识源尚未就绪：{raw}")
        return target

    def _prepare_workspace(self, job: dict[str, Any], source: Path) -> Path:
        with ki.source_operation_lock(self.project_root, source.name) as acquired:
            if not acquired:
                raise MiningJobError(f"知识源正在后处理，完成前不能创建任务：data/{source.name}")
            ingestion = ki.read_ingestion_state(
                self.project_root,
                source.name,
                has_documents=bool(_visible_files(source)),
            )
            if ingestion["status"] != "ready":
                raise MiningJobError(f"知识源尚未就绪：data/{source.name}")
            job_dir = self._job_dir(job["job_id"])
            workspace = job_dir / "workspace"
            workspace.mkdir(parents=True, exist_ok=False)
            data_target = workspace / "data" / "input"
            shutil.copytree(source, data_target)
        (workspace / "compiled_skill").mkdir()
        for name in _STATIC_WORKSPACE_ITEMS:
            source_item = self.project_root / name
            target_item = workspace / name
            if not source_item.exists():
                raise MiningJobError(f"SkillMiner 运行资源缺失：{name}")
            try:
                target_item.symlink_to(source_item, target_is_directory=source_item.is_dir())
            except OSError:
                if source_item.is_dir():
                    shutil.copytree(source_item, target_item)
                else:
                    shutil.copy2(source_item, target_item)
        job["workspace"] = workspace.relative_to(self.project_root).as_posix()
        return workspace

    def create_jobs(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(body, dict):
            raise MiningJobError("任务参数必须是对象")
        requests = body.get("jobs") if isinstance(body.get("jobs"), list) else [body]
        if not requests or len(requests) > 20:
            raise MiningJobError("单次可创建 1 到 20 个挖掘任务")
        created = []
        for request in requests:
            if not isinstance(request, dict):
                raise MiningJobError("任务配置必须是对象")
            source = self._source_dir(str(request.get("input_dir") or "data/input"))
            try:
                max_rounds = int(request.get("max_rounds") or 3)
            except (TypeError, ValueError) as exc:
                raise MiningJobError("max_rounds 必须是整数") from exc
            max_rounds = max(1, min(5, max_rounds))
            created_at = _utc_now()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            task_name = str(request.get("name") or request.get("task_name") or "").strip()
            if len(task_name) > 120:
                raise MiningJobError("任务名称不能超过 120 个字符")
            task_name = task_name or f"{source.name} 挖掘任务"
            job_id = f"mine-{stamp}-{_safe_slug(task_name)}-{uuid.uuid4().hex[:8]}"
            job = {
                "job_id": job_id,
                "name": task_name,
                "status": "preparing",
                "input_dir": f"data/{source.name}",
                "source_snapshot": "data/input",
                "document_count": len(_visible_files(source)),
                "max_rounds": max_rounds,
                "current_round": 0,
                "phase": {"step1": "idle", "step2": "idle", "step3": "idle"},
                "created_at": created_at,
                "started_at": "",
                "finished_at": "",
                "updated_at": created_at,
                "pid": None,
                "return_code": None,
                "error": "",
                "stop_reason": "",
                "artifact_quality": None,
                "human_checkpoints": bool(request.get("human_checkpoints", True)),
            }
            job_dir = self._job_dir(job_id)
            job_dir.mkdir(parents=True, exist_ok=False)
            try:
                self._prepare_workspace(job, source)
            except Exception:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise
            job["status"] = "queued"
            with self._lock:
                self._jobs[job_id] = job
                self._persist(job)
            created.append(self._public_job(job))
        self._dispatch()
        return [self.get_job(item["job_id"]) for item in created]

    def _dispatch(self) -> None:
        with self._lock:
            if self._shutting_down:
                return
            active = sum(
                1 for process in self._processes.values() if process.poll() is None
            )
            queued = sorted(
                (job for job in self._jobs.values() if job.get("status") == "queued"),
                key=lambda item: item.get("created_at", ""),
            )
            for job in queued[:max(0, self.max_parallel - active)]:
                try:
                    self._launch(job)
                except OSError as exc:
                    job["status"] = "failed"
                    job["finished_at"] = _utc_now()
                    job["error"] = f"启动流水线失败：{exc}"
                    self._persist(job)

    def _launch(self, job: dict[str, Any]) -> None:
        workspace = self.project_root / job["workspace"]
        pipeline = self.project_root / "run_pipeline.py"
        command = [
            self.python_executable,
            str(pipeline),
            "--workspace-root", str(workspace),
            "--input", "data/input",
            "--max-rounds", str(job["max_rounds"]),
        ]
        if job.get("human_checkpoints", True):
            command.extend([
                "--human-checkpoints",
                "--checkpoint-dir", str(self._job_dir(job["job_id"]) / "checkpoints"),
            ])
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        job["status"] = "running"
        job["started_at"] = _utc_now()
        job["pid"] = process.pid
        self._processes[job["job_id"]] = process
        self._persist(job)
        threading.Thread(
            target=self._monitor,
            args=(job["job_id"], process),
            daemon=True,
        ).start()

    def _append_log(self, job_id: str, line: str) -> None:
        log_path = self._job_dir(job_id) / "run.log"
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line.rstrip("\n") + "\n")

    def _update_progress(self, job: dict[str, Any], line: str) -> None:
        match = _PHASE_RE.search(line)
        changed = False
        if match:
            round_idx, step_idx = int(match.group(1)), int(match.group(2))
            phase_key = f"step{step_idx}"
            if round_idx != job.get("current_round"):
                job["current_round"] = round_idx
                job["phase"] = {"step1": "idle", "step2": "idle", "step3": "idle"}
            for idx in range(1, step_idx):
                job["phase"][f"step{idx}"] = "done"
            job["phase"][phase_key] = "active"
            changed = True
        if "编译产物契约校验通过" in line:
            job["phase"] = {"step1": "done", "step2": "done", "step3": "done"}
            changed = True
        if line.startswith("HUMAN_CHECKPOINT_WAITING::"):
            job["status"] = "waiting"
            changed = True
        if line.startswith("HUMAN_CHECKPOINT_ANSWERED::"):
            job["status"] = "running"
            changed = True
        if line.startswith("反思环终止原因:"):
            job["stop_reason"] = line.split(":", 1)[1].strip()
            changed = True
        if changed:
            self._persist(job)

    def _monitor(self, job_id: str, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._append_log(job_id, line)
            with self._lock:
                job = self._jobs.get(job_id)
                if job:
                    self._update_progress(job, line.rstrip())
        return_code = process.wait()
        with self._lock:
            job = self._jobs[job_id]
            was_stopping = job.get("status") == "stopping"
            job["return_code"] = return_code
            job["pid"] = None
            job["finished_at"] = _utc_now()
            if was_stopping:
                job["status"] = "stopped"
                job["stop_reason"] = job.get("stop_reason") or "用户手动停止"
            else:
                quality = self._assess_artifact_quality(job, return_code=return_code)
                job["artifact_quality"] = quality
                if quality["has_skill"]:
                    # A compiled Skill is a useful terminal result even when
                    # reflection stopped early or a Benchmark needed repair.
                    # Preserve it as a completed task and surface the exact
                    # review warnings instead of mislabelling it as a crash.
                    job["status"] = "succeeded"
                    job["error"] = ""
                    job["phase"] = {"step1": "done", "step2": "done", "step3": "done"}
                else:
                    job["status"] = "failed"
                    artifact_errors = self._completion_artifact_errors(job)
                    if artifact_errors:
                        job["error"] = "最终产物生成失败：" + "；".join(artifact_errors)
                    else:
                        job["error"] = f"流水线进程退出码：{return_code}"
            self._processes.pop(job_id, None)
            checkpoint_dir = self._job_dir(job_id) / "checkpoints"
            (checkpoint_dir / "pending.json").unlink(missing_ok=True)
            (checkpoint_dir / "answer.json").unlink(missing_ok=True)
            self._persist(job)
        self._dispatch()

    def _completion_artifact_errors(self, job: dict[str, Any]) -> list[str]:
        """Defend against a zero exit code with a non-submittable artifact set."""
        workspace = self.project_root / str(job.get("workspace") or "")
        compiled_root = workspace / "compiled_skill"
        skill_files = sorted(compiled_root.glob("*/SKILL.md")) if compiled_root.is_dir() else []
        if len(skill_files) != 1:
            return [f"应有且仅有一个 SKILL.md，实际为 {len(skill_files)} 个"]

        skill_dir = skill_files[0].parent
        errors = []
        for filename in ("EVALUATION.md", "BENCHMARK.md", "benchmark.json"):
            path = skill_dir / filename
            if not path.is_file() or path.stat().st_size == 0:
                errors.append(f"缺少 {filename}")
        benchmark_path = skill_dir / "benchmark.json"
        if not benchmark_path.is_file() or benchmark_path.stat().st_size == 0:
            return errors
        payload, format_errors = bf.read_document(benchmark_path)
        if payload is not None:
            format_errors.extend(bf.validate_document(payload, expected_skill_name=skill_dir.name))
        errors.extend(f"benchmark.json：{error}" for error in dict.fromkeys(format_errors))
        return errors

    def _assess_artifact_quality(self, job: dict[str, Any], *, return_code: int) -> dict[str, Any]:
        """Describe review risk without turning recoverable output into failure."""
        workspace = self.project_root / str(job.get("workspace") or "")
        compiled_root = workspace / "compiled_skill"
        skill_files = sorted(compiled_root.glob("*/SKILL.md")) if compiled_root.is_dir() else []
        if len(skill_files) != 1:
            return {
                "has_skill": False,
                "level": "incomplete",
                "label": "无可用最终 Skill",
                "confidence": "unknown",
                "summary": f"未生成唯一的 SKILL.md（实际 {len(skill_files)} 个）。",
                "warnings": [f"应有且仅有一个 SKILL.md，实际为 {len(skill_files)} 个"],
                "artifacts": [],
                "can_submit": False,
            }

        skill_dir = skill_files[0].parent
        text = skill_files[0].read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"(?:置信档|判定结果)\**\s*[：:]\s*\**\s*(生产级|候选级|草稿级)", text)
        confidence = match.group(1) if match else "unknown"
        warnings: list[str] = []
        artifacts: list[dict[str, str]] = []
        evaluation = skill_dir / "EVALUATION.md"
        benchmark_md = skill_dir / "BENCHMARK.md"
        benchmark_json = skill_dir / "benchmark.json"
        benchmark_quality = skill_dir / "benchmark_quality.json"

        artifacts.append({
            "kind": "skill",
            "state": "ready" if confidence in {"生产级", "候选级"} else "caution",
            "label": f"{confidence if confidence != 'unknown' else '未声明'} Skill",
            "detail": "Skill 已生成；请结合下方质量标记审核。",
        })
        if evaluation.is_file() and evaluation.stat().st_size:
            artifacts.append({"kind": "evaluation", "state": "ready", "label": "评测定义已生成", "detail": "可人工编辑与复核。"})
        else:
            warnings.append("缺少 EVALUATION.md，不能直接进入进化评审。")
            artifacts.append({"kind": "evaluation", "state": "incomplete", "label": "评测定义缺失", "detail": "请补充 EVALUATION.md 后再提交。"})

        if benchmark_md.is_file() and benchmark_md.stat().st_size and benchmark_json.is_file() and benchmark_json.stat().st_size:
            artifacts.append({"kind": "benchmark", "state": "ready", "label": "Benchmark 已生成", "detail": "题库文件与人读视图均已落盘。"})
            payload, format_errors = bf.read_document(benchmark_json)
            if payload is not None:
                format_errors.extend(bf.validate_document(payload, expected_skill_name=skill_dir.name))
            warnings.extend(f"Benchmark 格式：{error}" for error in dict.fromkeys(format_errors))
        else:
            warnings.append("缺少完整 Benchmark（benchmark.json 或 BENCHMARK.md）。")
            artifacts.append({"kind": "benchmark", "state": "incomplete", "label": "Benchmark 不完整", "detail": "可查看现有 Skill，但建议补齐题库后再提交。"})

        if benchmark_quality.is_file():
            try:
                report = json.loads(benchmark_quality.read_text(encoding="utf-8"))
                for item in report.get("warnings") or []:
                    if isinstance(item, dict):
                        message = str(item.get("message") or "").strip()
                    else:
                        message = str(item or "").strip()
                    if message:
                        warnings.append(message)
            except (OSError, json.JSONDecodeError):
                warnings.append("Benchmark 质量报告无法解析，请人工检查 benchmark_quality.json。")

        stop_reason = str(job.get("stop_reason") or "").strip()
        if stop_reason and any(token in stop_reason for token in ("未下降", "最大轮数", "无补充素材")):
            warnings.append(f"反思环提前结束：{stop_reason}")
        if return_code != 0:
            warnings.append(f"流水线以退出码 {return_code} 结束；已保留可用产物，请查看日志。")

        unique_warnings = list(dict.fromkeys(warnings))
        incomplete = any(item["state"] == "incomplete" for item in artifacts)
        if incomplete:
            level, label = "incomplete", "产物不完整"
        elif unique_warnings or confidence in {"草稿级", "unknown"}:
            level, label = "caution", "谨慎提交"
        elif confidence == "生产级":
            level, label = "ready", "可提交"
        else:
            level, label = "caution", "候选级 · 建议复核"
        summary = (
            "产物已生成，但存在需要人工复核的风险项。"
            if unique_warnings or incomplete
            else "产物完整，可进入进化评审。"
        )
        return {
            "has_skill": True,
            "level": level,
            "label": label,
            "confidence": confidence,
            "summary": summary,
            "warnings": unique_warnings,
            "artifacts": artifacts,
            "can_submit": bool(evaluation.is_file() and evaluation.stat().st_size),
        }

    def stop_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.get("status") == "queued":
                job["status"] = "stopped"
                job["finished_at"] = _utc_now()
                job["stop_reason"] = "用户在排队时停止"
                self._persist(job)
                return self._public_job(job, include_detail=True)
            process = self._processes.get(job_id)
            if job.get("status") not in {"running", "waiting", "stopping"} or process is None:
                raise MiningJobError("该任务当前不可停止")
            job["status"] = "stopping"
            self._persist(job)
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (AttributeError, ProcessLookupError, OSError):
                process.terminate()
            return self._public_job(job, include_detail=True)

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Permanently remove one terminal task and its isolated workspace.

        A job workspace contains the input snapshot, logs, checkpoints, and
        every mined artifact.  Active process groups must be stopped first:
        removing their workspace while they are writing would make the monitor
        race with the filesystem and leave an indeterminate task state.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.get("status") in _ACTIVE_STATES:
                raise MiningJobError("运行中的挖掘任务请先停止，停止完成后才能删除")
            process = self._processes.get(job_id)
            if process is not None and process.poll() is None:
                raise MiningJobError("挖掘进程仍在运行，请先停止任务")
            job_dir = self._job_dir(job_id).resolve()
            if job_dir.parent != self.jobs_root.resolve():
                raise MiningJobError("挖掘任务目录无效")
            artifact_count = sum(1 for path in job_dir.rglob("*") if path.is_file()) if job_dir.is_dir() else 0

            # Keep the registry intact if deletion fails, so the user can
            # retry instead of losing the only pointer to partially retained
            # output files.
            if job_dir.exists():
                shutil.rmtree(job_dir)
            self._jobs.pop(job_id, None)
            self._processes.pop(job_id, None)

        self._dispatch()
        return {
            "ok": True,
            "job_id": job_id,
            "deleted_files": artifact_count,
        }

    def submit_checkpoint_answer(
        self,
        job_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Resume one waiting task with answers keyed by checkpoint question id."""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job:
                raise KeyError(job_id)
            if job.get("status") != "waiting":
                raise MiningJobError("该挖掘任务当前没有待填写的知识补证")
            pending = self._pending_checkpoint(job)
            if not pending:
                raise MiningJobError("知识补证已结束，请刷新任务详情")
            if str(body.get("question_id") or "") != str(pending.get("id") or ""):
                raise MiningJobError("提问已过期，请刷新任务详情后重新填写")
            raw_answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
            allowed = {str(item.get("qid") or "") for item in pending.get("questions") or []}
            answers = {
                str(key): str(value).strip()
                for key, value in raw_answers.items()
                if str(key) in allowed and str(value).strip()
            }
            answer_path = self._job_dir(job["job_id"]) / "checkpoints" / "answer.json"
            _write_json_atomic(answer_path, {
                "question_id": pending["id"],
                "answers": answers,
                "stop": bool(body.get("stop")),
                "submitted_at": _utc_now(),
            })
            result = self._public_job(job, include_detail=True)
            result["status"] = "running"
            result["pending_checkpoint"] = None
            return result

    def _pending_checkpoint(self, job: dict[str, Any]) -> dict[str, Any] | None:
        path = self._job_dir(job["job_id"]) / "checkpoints" / "pending.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _knowledge_gaps(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Return durable gap diagnoses even after a checkpoint has closed.

        ``pending.json`` is intentionally transient and disappears after an
        answer, task stop, or service restart.  The semantic reports are the
        durable source of truth, so task detail rebuilds the same concrete gap
        list from those artifacts whenever the user opens the task.
        """
        workspace = self.project_root / str(job.get("workspace") or "")
        roots = []
        current = workspace / "semantic_reports"
        if current.is_dir():
            roots.append(current)
        rounds_root = workspace / "reflection_rounds"
        if rounds_root.is_dir():
            roots.extend(sorted(rounds_root.glob("*/round_*/semantic_reports"), reverse=True))

        questions = []
        seen: set[str] = set()
        for root in roots:
            extracted, _ = hc.extract_gap_questions_from_semantic_reports(root, limit=50)
            for question in extracted:
                key = str(question.get("qid") or question.get("question") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                questions.append(question)
        if not questions:
            return None
        return {"total": len(questions), "questions": questions}

    @staticmethod
    def _round_number(path: Path) -> int:
        try:
            return int(path.name.removeprefix("round_"))
        except ValueError:
            return 0

    def _knowledge_supplements(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """Return every knowledge-supplement round, including legacy jobs.

        New tasks persist the exact checkpoint form and submitted answers under
        ``checkpoints/history``. Older tasks did not keep that IPC history, so
        reconstruct their per-round questions from the archived semantic report
        instead of collapsing them into the final round's diagnosis.
        """
        workspace = self.project_root / str(job.get("workspace") or "")
        checkpoint_root = self._job_dir(job["job_id"]) / "checkpoints" / "history"
        rounds: list[dict[str, Any]] = []
        checkpoint_rounds: set[tuple[int, str]] = set()

        if checkpoint_root.is_dir():
            for path in sorted(checkpoint_root.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                questions = record.get("questions")
                if not isinstance(questions, list) or not questions:
                    continue
                round_idx = int(record.get("round") or 0)
                checkpoint = str(record.get("checkpoint") or "knowledge_supplement")
                checkpoint_rounds.add((round_idx, checkpoint))
                rounds.append({
                    "round": round_idx,
                    "checkpoint": checkpoint,
                    "title": str(record.get("title") or f"第 {round_idx} 轮知识补充"),
                    "intro": str(record.get("intro") or ""),
                    "questions": questions,
                    "answers": record.get("answers") if isinstance(record.get("answers"), dict) else {},
                    "submitted_at": str(record.get("submitted_at") or ""),
                    "source": "checkpoint",
                })

        archived_rounds: set[int] = set()
        rounds_root = workspace / "reflection_rounds"
        if rounds_root.is_dir():
            for reports_root in sorted(rounds_root.glob("*/round_*/semantic_reports")):
                round_idx = self._round_number(reports_root.parent)
                archived_rounds.add(round_idx)
                if (round_idx, "after_semantic") in checkpoint_rounds:
                    continue
                questions, total = hc.extract_gap_questions_from_semantic_reports(reports_root, limit=50)
                if questions:
                    rounds.append({
                        "round": round_idx,
                        "checkpoint": "after_semantic",
                        "title": f"第 {round_idx} 轮发现 {total} 个关键知识缺口",
                        "intro": "根据该轮归档的语义报告恢复；该历史任务未保留当时填写的答案。",
                        "questions": questions,
                        "answers": {},
                        "submitted_at": "",
                        "source": "semantic_report",
                    })

        # The latest round lives at the workspace root. Add it only when it
        # has not yet been archived, e.g. while a task is waiting for input.
        current_round = int(job.get("current_round") or 0)
        reports_root = workspace / "semantic_reports"
        if reports_root.is_dir() and current_round not in archived_rounds and (
            current_round,
            "after_semantic",
        ) not in checkpoint_rounds:
            questions, total = hc.extract_gap_questions_from_semantic_reports(reports_root, limit=50)
            if questions:
                rounds.append({
                    "round": current_round,
                    "checkpoint": "after_semantic",
                    "title": f"第 {current_round or 1} 轮发现 {total} 个关键知识缺口",
                    "intro": "根据当前轮的语义报告生成。",
                    "questions": questions,
                    "answers": {},
                    "submitted_at": "",
                    "source": "semantic_report",
                })

        return sorted(rounds, key=lambda item: (
            int(item.get("round") or 0),
            str(item.get("submitted_at") or ""),
            str(item.get("checkpoint") or ""),
        ))

    def _artifact_rows(self, root: Path) -> list[dict[str, Any]]:
        rows = []
        if not root.is_dir():
            return rows
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            kind = _ARTIFACT_NAMES.get(path.name)
            if kind is None and "semantic_reports" in path.parts and path.suffix.lower() == ".md":
                kind = "semantic"
            if kind is None:
                continue
            rows.append({
                "name": path.name,
                "kind": kind,
                "path": path.relative_to(self.project_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "skill_name": path.parent.name if "compiled_skill" in path.parts else "",
            })
        return rows

    def _artifacts(self, job: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        workspace = self.project_root / str(job.get("workspace") or "")
        final = self._artifact_rows(workspace / "compiled_skill")
        final += self._artifact_rows(workspace / "semantic_reports")
        rounds = []
        rounds_root = workspace / "reflection_rounds"
        if rounds_root.is_dir():
            for round_dir in sorted(rounds_root.glob("*/round_*")):
                try:
                    round_idx = int(round_dir.name.removeprefix("round_"))
                except ValueError:
                    continue
                rounds.append({"round": round_idx, "artifacts": self._artifact_rows(round_dir)})
        return final, rounds

    def _read_logs(self, job_id: str, limit: int = 400) -> list[str]:
        path = self._job_dir(job_id) / "run.log"
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]

    def _public_job(self, job: dict[str, Any], *, include_detail: bool = False) -> dict[str, Any]:
        hidden = {"pid", "return_code", "workspace"}
        result = {key: value for key, value in job.items() if key not in hidden}
        if include_detail:
            final, rounds = self._artifacts(job)
            result["artifacts"] = final
            result["rounds"] = rounds
            result["logs"] = self._read_logs(job["job_id"])
            pending = self._pending_checkpoint(job)
            result["pending_checkpoint"] = pending
            result["knowledge_gaps"] = self._knowledge_gaps(job)
            result["knowledge_supplements"] = self._knowledge_supplements(job)
            if pending and result.get("status") == "running":
                result["status"] = "waiting"
        return result

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._public_job(job) for job in self._jobs.values()]
        return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return self._public_job(job, include_detail=True)

    def summary(self) -> dict[str, Any]:
        jobs = self.list_jobs()
        return {
            "total": len(jobs),
            "running": sum(job["status"] in {"running", "waiting", "stopping"} for job in jobs),
            "queued": sum(job["status"] in {"queued", "preparing"} for job in jobs),
            "completed": sum(job["status"] == "succeeded" for job in jobs),
            "failed": sum(job["status"] in {"failed", "interrupted"} for job in jobs),
            "max_parallel": self.max_parallel,
        }

    def shutdown(self) -> None:
        """Stop running process groups while preserving queued tasks for restart."""
        with self._lock:
            self._shutting_down = True
            targets = []
            for job_id, process in list(self._processes.items()):
                if process.poll() is not None:
                    continue
                job = self._jobs.get(job_id)
                if job:
                    job["status"] = "stopping"
                    job["stop_reason"] = "挖掘服务停止"
                    self._persist(job)
                targets.append(process)
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except (AttributeError, ProcessLookupError, OSError):
                    process.terminate()
        deadline = datetime.now(timezone.utc).timestamp() + 5
        for process in targets:
            timeout = max(0.1, deadline - datetime.now(timezone.utc).timestamp())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (AttributeError, ProcessLookupError, OSError):
                    process.kill()


__all__ = ["MiningJobError", "MiningJobManager"]
