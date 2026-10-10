"""OpenViking Compile orchestration for Skill mining.

The module deliberately owns only local control-plane state and artifact
materialisation.  OpenViking I/O stays in the FastAPI bridge, where account
headers and request-scoped authentication already live.  Completed bundles are
written into the historical ``mining_jobs/<job>/workspace`` layout so the
existing Candidate Review and True Replay hand-off remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from team_skills.library.frontmatter import parse_skill_md

from . import benchmark_format
from .compile_strategy import compiler_execution_guidance

SKILL_NAME = "skill-miner-compile-v1"
SKILL_URI = f"viking://agent/skills/{SKILL_NAME}"
RUNS_ROOT = "viking://resources/agent_skill_workspace/runs"
SOURCE_TAG_SKILL = "skill"
SOURCE_TAG_KNOWLEDGE = "knowledge"
SOURCE_TAGS = frozenset({SOURCE_TAG_SKILL, SOURCE_TAG_KNOWLEDGE})
DEFAULT_KNOWLEDGE_SOURCE_URI = (
    "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base"
)
ACTIVE_JOB_STATES = {"preparing", "queued", "running", "stopping"}
TERMINAL_JOB_STATES = {"succeeded", "failed", "stopped", "interrupted"}
MAX_PARALLEL_JOBS = 8

SKILL_BODY = """---
name: skill-miner-compile-v1
description: Compile an immutable source snapshot into one evidence-grounded Skill Candidate and its Test Dataset.
---

# Skill Candidate Compiler

Read every relevant document in the supplied source snapshot and produce one
reviewable Skill Candidate.  This compiler creates candidates only; it never
publishes or installs the resulting Skill.

## Output contract

Write the following files below the requested output directory:

- `analysis/evidence-map.md`: important claims mapped to exact source Viking URIs.
- `analysis/gaps.md`: contradictions, missing evidence, and questions that need human supplementation.
- `compiled_skill/<skill-name>/SKILL.md`: a valid reusable Skill with YAML `name` and `description` frontmatter.
- `compiled_skill/<skill-name>/EVALUATION.md`: scope, positive/negative examples, risks, and acceptance criteria.
- `compiled_skill/<skill-name>/benchmark.json`: one `team-replay.dataset` document with 12-24 diverse test cases.
  The document must contain `schema_version` (exactly `team-replay.dataset`), a nonempty
  `dataset_id` and `name`, `description`, `skills` ([{"skill_id": "<skill-name>"}]),
  `source` ({"kind": "skill_miner"}), `cases`, ISO `created_at` and `updated_at`,
  and `metadata` ({"candidate_revision": 1}).
  Each case must contain exactly `case_id`, `query`, `skill_ids` (["<skill-name>"]),
  `checks`, `materials` (an array), `provenance`, `replay`, and `metadata`.
  `checks` must contain at least 12 distinct concrete output completion conditions
  and at least one trajectory condition, each with unique `id`, `kind` (`output` or
  `trajectory`), and `text`; prohibitions may include `polarity`: `must_not`.
  Put source Viking URIs in `provenance.session_ids`.
  Set `replay.progressive_disclosure` to {"enabled": true, "initial_visibility":
  "query_only", "batch_size": 4, "stop_when": "all_checklist_items_satisfied"}.
  Put `split`: `test`, `minimum_requirement_target`: 12, `difficulty`,
  `target_dimensions`, and the display `name` in case `metadata`.
  Do not emit alternative formats, `input`, `requirements`, or `name` at case top level.
- `compiled_skill/<skill-name>/BENCHMARK.md`: a human-readable view of the same test cases.

## Quality rules

- Produce exactly one Skill Candidate and use a lowercase kebab-case skill name.
- Cite source Viking URIs near material claims; never invent facts or policies.
- Synthesize stable procedures and decision rules instead of summarising files.
- State conflicts and uncertainty explicitly in `analysis/gaps.md`.
- Include boundary, refusal, ambiguity, and adversarial cases in the test set.
- Do not write outside the requested output directory.
"""


SKILL_BODY += compiler_execution_guidance()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_slug(value: Any, fallback: str = "skill-mining") -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9\-]+", "-", raw)
    return "-".join(part for part in slug.split("-") if part)[:48] or fallback


def source_id(account: str, uri: str) -> str:
    digest = hashlib.sha256(f"{account}|{uri.rstrip('/')}".encode("utf-8")).hexdigest()[:20]
    return f"ovsrc-{digest}"


def normalize_source_tags(value: Any, *, default: tuple[str, ...] = ()) -> list[str]:
    """Return stable source-purpose tags and reject unknown or empty selections."""
    raw = default if value is None else value
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError("知识源用途标签格式无效")
    tags: list[str] = []
    for item in raw:
        tag = str(item or "").strip().lower()
        if tag not in SOURCE_TAGS:
            raise ValueError(f"不支持的知识源用途标签：{tag or '(empty)'}")
        if tag not in tags:
            tags.append(tag)
    if not tags:
        raise ValueError("请至少选择一种知识源用途")
    return [tag for tag in (SOURCE_TAG_SKILL, SOURCE_TAG_KNOWLEDGE) if tag in tags]


def source_has_tag(record: dict[str, Any], tag: str) -> bool:
    # Records created before the unified source registry were Skill-only.
    return tag in normalize_source_tags(record.get("tags"), default=(SOURCE_TAG_SKILL,))


def validate_resource_uri(value: Any, *, allow_root: bool = False) -> str:
    uri = str(value or "").strip().rstrip("/")
    prefix = "viking://resources"
    if (not allow_root and uri == prefix) or not (uri == prefix or uri.startswith(f"{prefix}/")):
        raise ValueError("知识源必须是 OpenViking Resources 下的目录")
    relative = uri[len(prefix) :].lstrip("/")
    if len(uri) > 1000 or any(part in {"", ".", ".."} for part in relative.split("/") if relative):
        raise ValueError("OpenViking 目录路径无效")
    return uri


def validate_descendant(root_uri: str, value: Any, *, allow_root: bool = True) -> str:
    root = validate_resource_uri(root_uri)
    uri = validate_resource_uri(value)
    if uri == root and allow_root:
        return uri
    if not uri.startswith(f"{root}/"):
        raise ValueError("文件路径不在所选知识源目录内")
    return uri


class OvSkillMiningRepository:
    """Thread-safe local registry for OV source references and mining jobs."""

    def __init__(self, state_root: Path | str, project_root: Path | str) -> None:
        self.state_root = Path(state_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.jobs_root = self.project_root / "mining_jobs"
        self.sources_path = self.state_root / "sources.json"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _load_sources(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        rows = payload.get("sources") if isinstance(payload, dict) else None
        normalized: list[dict[str, Any]] = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["tags"] = normalize_source_tags(
                row.get("tags"),
                default=(SOURCE_TAG_SKILL,),
            )
            row["default"] = bool(row.get("default"))
            normalized.append(row)
        return normalized

    def _save_sources(self, rows: list[dict[str, Any]]) -> None:
        _atomic_json(self.sources_path, {"schema_version": 2, "sources": rows})

    def list_source_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load_sources(), key=lambda row: str(row.get("created_at") or ""))

    def get_source(self, identity: str) -> dict[str, Any]:
        with self._lock:
            match = next((row for row in self._load_sources() if row.get("source_id") == identity), None)
        if match is None:
            raise KeyError(identity)
        return match

    def save_source(
        self,
        account: str,
        uri: str,
        *,
        name: str = "",
        tags: Any = None,
        replace_tags: bool = False,
        default: bool = False,
    ) -> dict[str, Any]:
        normalized = validate_resource_uri(uri)
        identity = source_id(account, normalized)
        requested_tags = normalize_source_tags(tags, default=(SOURCE_TAG_SKILL,))
        if default and SOURCE_TAG_KNOWLEDGE not in requested_tags:
            requested_tags.append(SOURCE_TAG_KNOWLEDGE)
        with self._lock:
            rows = self._load_sources()
            existing = next((row for row in rows if row.get("source_id") == identity), None)
            if existing:
                current_tags = normalize_source_tags(existing.get("tags"), default=(SOURCE_TAG_SKILL,))
                next_tags = requested_tags if replace_tags else list(dict.fromkeys([*current_tags, *requested_tags]))
                if existing.get("default") or default:
                    next_tags = list(dict.fromkeys([*next_tags, SOURCE_TAG_KNOWLEDGE]))
                next_tags = normalize_source_tags(next_tags)
                changed = next_tags != current_tags or (default and not existing.get("default"))
                if name and str(name).strip() != str(existing.get("name") or ""):
                    existing["name"] = str(name).strip()
                    changed = True
                if changed:
                    existing["tags"] = next_tags
                    existing["default"] = bool(existing.get("default") or default)
                    existing["updated_at"] = utc_now()
                    self._save_sources(rows)
                return dict(existing)
            now = utc_now()
            record = {
                "source_id": identity,
                "account": str(account),
                "uri": normalized,
                "name": str(name or normalized.rsplit("/", 1)[-1]),
                "tags": normalize_source_tags(requested_tags),
                "default": bool(default),
                "created_at": now,
                "updated_at": now,
            }
            rows.append(record)
            self._save_sources(rows)
            return dict(record)

    def ensure_default_knowledge_source(self, account: str) -> dict[str, Any]:
        identity = source_id(account, DEFAULT_KNOWLEDGE_SOURCE_URI)
        with self._lock:
            exists = any(row.get("source_id") == identity for row in self._load_sources())
        return self.save_source(
            account,
            DEFAULT_KNOWLEDGE_SOURCE_URI,
            name="" if exists else "默认知识库知识源",
            tags=[SOURCE_TAG_KNOWLEDGE],
            default=True,
        )

    def update_source(self, identity: str, *, tags: Any, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            rows = self._load_sources()
            match = next((row for row in rows if row.get("source_id") == identity), None)
            if match is None:
                raise KeyError(identity)
            next_tags = normalize_source_tags(tags)
            if match.get("default") and SOURCE_TAG_KNOWLEDGE not in next_tags:
                raise ValueError("默认知识库知识源必须保留“知识库知识源”标签")
            match["tags"] = next_tags
            if name is not None and str(name).strip():
                match["name"] = str(name).strip()
            match["updated_at"] = utc_now()
            self._save_sources(rows)
            return dict(match)

    def remove_source(self, identity: str) -> dict[str, Any]:
        with self._lock:
            rows = self._load_sources()
            match = next((row for row in rows if row.get("source_id") == identity), None)
            if match is None:
                raise KeyError(identity)
            if match.get("default"):
                raise ValueError("默认知识库知识源不能移除")
            self._save_sources([row for row in rows if row.get("source_id") != identity])
            return match

    def _job_dir(self, job_id: str) -> Path:
        candidate = (self.jobs_root / str(job_id)).resolve()
        if candidate.parent != self.jobs_root.resolve():
            raise ValueError("挖掘任务 ID 无效")
        return candidate

    def create_job(
        self,
        *,
        name: str,
        account: str,
        source_uri: str,
        source_snapshot_uri: str,
        output_uri: str,
        document_count: int,
        max_rounds: int,
        runtime_timeout_seconds: int = 3600,
        human_checkpoints: bool = True,
    ) -> dict[str, Any]:
        if not 60 <= runtime_timeout_seconds <= 7200:
            raise ValueError("运行期限必须在 60 到 7200 秒之间")
        with self._lock:
            active = sum(
                1
                for row in self._iter_jobs_unlocked()
                if row.get("engine") == "ov_compile" and row.get("status") in ACTIVE_JOB_STATES
            )
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError(f"同时最多运行 {MAX_PARALLEL_JOBS} 个 OV Compile 挖掘任务")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            job_id = f"ovmine-{stamp}-{_safe_slug(name)}-{uuid.uuid4().hex[:8]}"
            now = utc_now()
            normalized_rounds = max(1, min(5, int(max_rounds)))
            job = {
                "job_id": job_id,
                "name": str(name or f"{source_uri.rsplit('/', 1)[-1]} 挖掘任务")[:120],
                "engine": "ov_compile",
                "status": "preparing",
                "input_dir": source_uri,
                "account": account,
                "source_uri": source_uri,
                "source_snapshot": source_snapshot_uri,
                "output_uri": output_uri,
                "skill_uri": SKILL_URI,
                "ov_task_id": "",
                "document_count": int(document_count),
                "max_rounds": normalized_rounds,
                "current_round": None,
                "runtime_timeout_seconds": runtime_timeout_seconds,
                "human_checkpoints_requested": bool(human_checkpoints),
                "human_checkpoints_effective": False,
                "rounds_mode": "instruction_hint",
                "options_warnings": ["OV Compile 不支持暂停表单问答；轮数仅作为内部复核提示，不代表已执行轮次"],
                "ov_cancel_requested_at": "",
                "ov_cancel_reason": "",
                "ov_cancel_sent_at": "",
                "ov_cancel_error": "",
                "phase": {"step1": "active", "step2": "idle", "step3": "idle"},
                "created_at": now,
                "started_at": "",
                "finished_at": "",
                "updated_at": now,
                "error": "",
                "stop_reason": "",
                "artifact_quality": None,
                "materialized": False,
                "ov_last_synced_at": "",
                "ov_last_sync_attempt_at": "",
                "ov_sync_error": "",
                "ov_sync_failures": 0,
                "ov_next_retry_at": "",
            }
            job_dir = self._job_dir(job_id)
            (job_dir / "workspace" / "compiled_skill").mkdir(parents=True, exist_ok=False)
            (job_dir / "workspace" / "semantic_reports").mkdir(parents=True, exist_ok=True)
            job["workspace"] = (job_dir / "workspace").relative_to(self.project_root).as_posix()
            _atomic_json(job_dir / "job.json", job)
            return dict(job)

    def _iter_jobs_unlocked(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.jobs_root.glob("*/job.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def list_jobs(self, *, engine_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._iter_jobs_unlocked()
        if engine_only:
            rows = [row for row in rows if row.get("engine") == "ov_compile"]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)

    def get_job(self, job_id: str) -> dict[str, Any]:
        path = self._job_dir(job_id) / "job.json"
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(job_id) from exc
        if not isinstance(row, dict):
            raise KeyError(job_id)
        return row

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            row = self.get_job(job_id)
            row.update(changes)
            row["updated_at"] = utc_now()
            _atomic_json(self._job_dir(job_id) / "job.json", row)
            return row

    def append_log(self, job_id: str, message: str) -> None:
        line = str(message).replace("\r", " ").rstrip("\n")
        if not line:
            return
        path = self._job_dir(job_id) / "run.log"
        with self._lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"[{utc_now()}] {line}\n")

    def delete_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.get_job(job_id)
            if row.get("status") in ACTIVE_JOB_STATES:
                raise RuntimeError("运行中的挖掘任务请先停止")
            job_dir = self._job_dir(job_id)
            count = sum(1 for path in job_dir.rglob("*") if path.is_file())
            shutil.rmtree(job_dir)
            return {"ok": True, "job_id": job_id, "deleted_files": count}

    def workspace(self, job: dict[str, Any]) -> Path:
        path = (self.project_root / str(job.get("workspace") or "")).resolve()
        if self.project_root not in path.parents:
            raise ValueError("挖掘任务工作区无效")
        return path

    def write_output(self, job_id: str, relative_path: str, content: str) -> Path:
        job = self.get_job(job_id)
        workspace = self.workspace(job)
        raw = str(PurePosixPath(relative_path))
        parts = [part for part in raw.split("/") if part]
        if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
            raise ValueError("OV Compile 返回了无效产物路径")
        if parts[0] == "analysis":
            parts[0] = "semantic_reports"
        elif parts[0] != "compiled_skill":
            if parts[-1] in {"SKILL.md", "EVALUATION.md", "BENCHMARK.md", "benchmark.json"}:
                parts.insert(0, "compiled_skill")
        target = (workspace / Path(*parts)).resolve()
        if workspace not in target.parents:
            raise ValueError("OV Compile 产物路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def finalize_materialized(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        workspace = self.workspace(job)
        for name in ("evidence-map.md", "gaps.md"):
            report = workspace / "semantic_reports" / name
            if not report.is_file() or not report.read_text(encoding="utf-8").strip():
                raise ValueError(f"OV Compile 未生成 analysis/{name}")
        skill_files = sorted((workspace / "compiled_skill").glob("*/SKILL.md"))
        if len(skill_files) != 1:
            raise ValueError(f"OV Compile 应生成一个 Skill Candidate，实际找到 {len(skill_files)} 个")
        skill_dir = skill_files[0].parent
        parsed = parse_skill_md(str(skill_files[0]))
        if not parsed or str(parsed.get("name") or "") != skill_dir.name:
            raise ValueError("SKILL.md frontmatter 的 name 必须与目录名一致")
        evaluation = skill_dir / "EVALUATION.md"
        if not evaluation.is_file() or not evaluation.stat().st_size:
            raise ValueError("OV Compile 未生成 EVALUATION.md")

        benchmark_path = skill_dir / "benchmark.json"
        warnings: list[str] = []
        if not benchmark_path.is_file():
            raise ValueError("OV Compile 未生成 benchmark.json")
        document, errors = benchmark_format.read_document(benchmark_path)
        if document is not None:
            errors.extend(benchmark_format.validate_document(document, expected_skill_name=skill_dir.name))
        if errors:
            raise ValueError("Test Dataset 格式不合法：" + "；".join(dict.fromkeys(errors)))
        case_count = len((document or {}).get("cases") or [])
        if not 12 <= case_count <= 24:
            raise ValueError(f"Test Dataset 应有 12–24 条案例，实际为 {case_count} 条")
        for case in (document or {}).get("cases") or []:
            citations = (case.get("provenance") or {}).get("session_ids") or []
            if not citations or any(not isinstance(uri, str) or not uri.startswith("viking://") for uri in citations):
                raise ValueError("每条 Test Dataset 案例必须包含非空的来源 Viking URI")
            for uri in citations:
                if not any(
                    str(uri).startswith(str(job.get(key) or "") + "/")
                    for key in ("source_uri", "source_snapshot") if job.get(key)
                ):
                    raise ValueError("Test Dataset 引用不属于本任务知识源")
        benchmark_md = skill_dir / "BENCHMARK.md"
        if not benchmark_md.is_file() or not benchmark_md.read_text(encoding="utf-8").strip():
            raise ValueError("OV Compile 未生成完整的 BENCHMARK.md，禁止自动补齐必需产物")
        question_count = len((document or {}).get("cases") or [])
        quality = {
            "has_skill": True,
            "level": "caution" if warnings else "ready",
            "label": "需复核" if warnings else "可提交",
            "confidence": "候选级",
            "summary": "OV Compile 候选已物化，并通过 Test Dataset 结构校验。",
            "warnings": list(dict.fromkeys(item for item in warnings if item)),
            "artifacts": [
                {"kind": "skill", "state": "ready", "label": "Skill Candidate 已生成", "detail": "等待候选评审。"},
                {"kind": "evaluation", "state": "ready", "label": "评测定义已生成", "detail": "可人工编辑。"},
                {
                    "kind": "benchmark",
                    "state": "ready",
                    "label": f"Test Dataset · {question_count} 条",
                    "detail": "已通过 Test Dataset 结构校验。",
                },
            ],
            "can_submit": True,
        }
        return self.update_job(job_id, materialized=True, artifact_quality=quality)

    def public_job(self, job: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        hidden = {"workspace"}
        result = {key: value for key, value in job.items() if key not in hidden}
        result["remote_state_stale"] = bool(
            job.get("engine") == "ov_compile"
            and job.get("status") in ACTIVE_JOB_STATES
            and job.get("ov_sync_error")
        )
        if not detail:
            return result
        workspace = self.workspace(job)
        artifacts: list[dict[str, Any]] = []
        kinds = {
            "SKILL.md": "skill",
            "EVALUATION.md": "evaluation",
            "BENCHMARK.md": "benchmark",
            "benchmark.json": "benchmark",
        }
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            kind = kinds.get(path.name)
            if kind is None and "semantic_reports" in path.parts:
                kind = "semantic"
            if kind is None:
                continue
            artifacts.append(
                {
                    "name": path.name,
                    "kind": kind,
                    "path": path.relative_to(self.project_root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "skill_name": path.parent.name if "compiled_skill" in path.parts else "",
                }
            )
        log_path = self._job_dir(job["job_id"]) / "run.log"
        logs = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if log_path.is_file() else []
        skills = []
        for skill_file in sorted((workspace / "compiled_skill").glob("*/SKILL.md")):
            skill_dir = skill_file.parent
            document, _ = benchmark_format.read_document(skill_dir / "benchmark.json")
            skills.append(
                {
                    "name": skill_dir.name,
                    "has_skill": True,
                    "has_evaluation": (skill_dir / "EVALUATION.md").is_file(),
                    "has_benchmark": (skill_dir / "benchmark.json").is_file(),
                    "question_count": len((document or {}).get("cases") or []),
                }
            )
        result.update(
            {
                "artifacts": artifacts,
                "rounds": [],
                "logs": logs,
                "skills": skills,
                "pending_checkpoint": None,
                "knowledge_gaps": None,
                "knowledge_supplements": [],
            }
        )
        return result

    def summary(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(jobs),
            "running": sum(job.get("status") in {"running", "stopping"} for job in jobs),
            "queued": sum(job.get("status") in {"preparing", "queued"} for job in jobs),
            "completed": sum(job.get("status") == "succeeded" for job in jobs),
            "failed": sum(job.get("status") in {"failed", "interrupted", "stopped"} for job in jobs),
            "max_parallel": MAX_PARALLEL_JOBS,
        }


__all__ = [
    "ACTIVE_JOB_STATES",
    "MAX_PARALLEL_JOBS",
    "OvSkillMiningRepository",
    "RUNS_ROOT",
    "DEFAULT_KNOWLEDGE_SOURCE_URI",
    "SOURCE_TAG_KNOWLEDGE",
    "SOURCE_TAG_SKILL",
    "SKILL_BODY",
    "SKILL_NAME",
    "SKILL_URI",
    "TERMINAL_JOB_STATES",
    "source_id",
    "source_has_tag",
    "normalize_source_tags",
    "utc_now",
    "validate_descendant",
    "validate_resource_uri",
]
