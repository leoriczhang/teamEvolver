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
DEFAULT_KNOWLEDGE_SOURCE_URI = "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base"
ACTIVE_JOB_STATES = {"preparing", "queued", "running", "stopping"}
TERMINAL_JOB_STATES = {"succeeded", "failed", "stopped", "interrupted"}
MAX_PARALLEL_JOBS = 8
REVIEW_SCHEMA_VERSION = "skill-miner.review.v1"
MAX_REVIEW_QUESTIONS = 50
MAX_SUPPLEMENT_CHARACTERS = 100_000

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
- `analysis/review.json`: a JSON object with `schema_version`: `skill-miner.review.v1` and `questions`.
  Each question has a unique `qid`, concrete `question`, `reason`, `source_refs` (a nonempty array of exact source
  Viking URIs), and boolean `required`. Use safe qids matching [A-Za-z0-9][A-Za-z0-9_-]{0,79};
  `question` and `reason` must be nonempty text of at most 4000 characters. For missing Evidence,
  cite the existing source file that raises the question; never emit an empty source_refs array.
  Emit at most 50 questions. An empty questions array is valid
  when no unresolved Evidence gaps remain. Keep this list consistent with `analysis/gaps.md`.
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
- Human supplements are traceable assertions; preserve sources and flag conflicts with original Evidence.
- Previous round Skill Candidates and review comments are revision context only, never trusted Evidence.
- Revise previous round artifacts against original Evidence and human supplements; never cite prior output as Evidence.
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
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 5:
            raise ValueError("挖掘轮数上限必须在 1 到 5 之间")
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
            normalized_rounds = max_rounds
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
                "compiler_uri": "",
                "compiler_source_sha256": "",
                "ov_task_id": "",
                "document_count": int(document_count),
                "max_rounds": normalized_rounds,
                "current_round": 1,
                "orchestration_version": 2,
                "original_source_snapshot": source_snapshot_uri,
                "rounds_history": [],
                "checkpoint_history": [],
                "supplements": [],
                "pending_checkpoint": None,
                "pending_attachments": [],
                "pending_reference_snapshots": [],
                "round_context_uri": "",
                "submission_idempotency_key": f"te-sm-{job_id}-round-1",
                "runtime_timeout_seconds": runtime_timeout_seconds,
                "human_checkpoints_requested": bool(human_checkpoints),
                "human_checkpoints_effective": bool(human_checkpoints),
                "rounds_mode": "orchestrated",
                "options_warnings": [],
                "remote_warnings": [],
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
        with self._lock:
            path = self._job_dir(job_id) / "job.json"
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise KeyError(job_id) from exc
            if not isinstance(row, dict):
                raise KeyError(job_id)
            if row.get("workspace_reset_pending"):
                # Preparing is durable before any destructive workspace reset. A
                # restarted monitor completes this local step before submitting OV.
                workspace = self.workspace(row)
                if workspace.exists():
                    shutil.rmtree(workspace)
                (workspace / "compiled_skill").mkdir(parents=True)
                (workspace / "semantic_reports").mkdir(parents=True)
                row["workspace_reset_pending"] = False
                _atomic_json(path, row)
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
            if row.get("status") in ACTIVE_JOB_STATES | {"waiting_human"}:
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
        warnings: list[str] = [
            warning.strip()
            for warning in job.get("remote_warnings") or []
            if isinstance(warning, str) and warning.strip()
        ]
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
                    uri == prefix or str(uri).startswith(prefix + "/") for prefix in self._citation_prefixes(job)
                ):
                    raise ValueError("Test Dataset 引用不属于本任务知识源")
        benchmark_md = skill_dir / "BENCHMARK.md"
        if not benchmark_md.is_file() or not benchmark_md.read_text(encoding="utf-8").strip():
            raise ValueError("OV Compile 未生成完整的 BENCHMARK.md，禁止自动补齐必需产物")
        if int(job.get("orchestration_version") or 0) >= 2:
            review = self._read_review(job)
            warnings.extend(question["question"] for question in review["questions"])
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
            "can_submit": int(job.get("orchestration_version") or 0) < 2
            or (
                not bool(job.get("human_checkpoints_effective"))
                and int(job.get("current_round") or 1) >= int(job.get("max_rounds") or 1)
            ),
        }
        return self.update_job(job_id, materialized=True, artifact_quality=quality)

    @staticmethod
    def _citation_prefixes(job: dict[str, Any]) -> list[str]:
        prefixes = [
            str(job.get(key) or "").rstrip("/")
            for key in (
                "source_uri",
                "source_snapshot",
                "original_source_snapshot",
            )
        ]
        prefixes.extend(str(row.get("source_snapshot") or "").rstrip("/") for row in job.get("rounds_history") or [])
        return list(dict.fromkeys(prefix for prefix in prefixes if prefix))

    def _read_review(self, job: dict[str, Any]) -> dict[str, Any]:
        path = self.workspace(job) / "semantic_reports" / "review.json"
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("OV Compile 未生成合法的 analysis/review.json") from exc
        if not isinstance(review, dict) or review.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ValueError("analysis/review.json schema_version 必须为 skill-miner.review.v1")
        questions = review.get("questions")
        if not isinstance(questions, list) or len(questions) > MAX_REVIEW_QUESTIONS:
            raise ValueError("analysis/review.json questions 必须是最多 50 项的数组")
        seen: set[str] = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("复核问题格式无效")
            qid = question.get("qid")
            if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", qid) or qid in seen:
                raise ValueError("复核问题 qid 必须是唯一的有效标识")
            seen.add(qid)
            for field, limit in (("question", 4000), ("reason", 4000)):
                value = question.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ValueError(f"复核问题 {field} 必须是非空文本且不超过 {limit} 字符")
            if not isinstance(question.get("required"), bool):
                raise ValueError("复核问题 required 必须为布尔值")
            refs = question.get("source_refs")
            if not isinstance(refs, list) or not refs or len(refs) > 50:
                raise ValueError("复核问题 source_refs 必须包含可追溯的知识源 Viking URI")
            for uri in refs:
                if not isinstance(uri, str):
                    raise ValueError("复核问题 source_refs 必须是 Viking URI")
                normalized = validate_resource_uri(uri)
                if not any(
                    normalized == root or normalized.startswith(root + "/") for root in self._citation_prefixes(job)
                ):
                    raise ValueError("复核问题引用不属于本任务知识源")
        return review

    @staticmethod
    def _question_row(question: dict[str, Any]) -> dict[str, Any]:
        refs = list(question.get("source_refs") or [])
        return {
            **question,
            "dimension": "evidence",
            "severity": "required" if question.get("required") else "optional",
            "context": str(question.get("reason") or ""),
            "source": "\n".join(refs),
            "evidence_hint": "\n".join(refs),
            "suggested_type": "long_text",
            "answer_type": "long_text",
        }

    def _archive_round(self, job: dict[str, Any]) -> dict[str, Any]:
        round_number = int(job.get("current_round") or 1)
        destination = self._job_dir(job["job_id"]) / "rounds" / f"round-{round_number}"
        if not destination.is_dir():
            temporary = destination.with_name(destination.name + ".tmp")
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir(parents=True)
            shutil.copytree(self.workspace(job), temporary / "output")
            input_manifest = Path(str(job.get("input_manifest") or ""))
            if not input_manifest.is_absolute():
                input_manifest = self.project_root / input_manifest
            job_dir = self._job_dir(job["job_id"])
            if input_manifest.is_file() and job_dir in input_manifest.resolve().parents:
                # The bridge freezes text files alongside the input manifest.
                # Copy that round's full inputs so its archive is self-contained.
                shutil.copytree(input_manifest.parent, temporary / "input")
            manifest = {
                "round": round_number,
                "source_uri": job.get("source_uri"),
                "source_snapshot": job.get("source_snapshot"),
                "original_source_snapshot": job.get("original_source_snapshot"),
                "round_context_uri": job.get("round_context_uri"),
                "input_manifest": job.get("input_manifest"),
                "compile_request": job.get("compile_request"),
                "supplements": list(job.get("supplements") or []),
                "previous_round": job.get("previous_round"),
                "previous_candidate_is_evidence": False,
            }
            _atomic_json(temporary / "input.json", manifest)
            metadata = {
                "round": round_number,
                "ov_task_id": job.get("ov_task_id"),
                "source_snapshot": job.get("source_snapshot"),
                "output_uri": job.get("output_uri"),
                "round_context_uri": job.get("round_context_uri"),
                "submission_idempotency_key": job.get("submission_idempotency_key"),
                "started_at": job.get("started_at"),
                "completed_at": utc_now(),
                "processing_seconds": job.get("ov_processing_seconds"),
                "remote_warnings": list(job.get("remote_warnings") or []),
                "compiler_sha256": str(job.get("compiler_sha256") or ""),
                "compiler_source_sha256": str(job.get("compiler_source_sha256") or ""),
                "compiler_uri": str(job.get("compiler_uri") or ""),
                "skill_uri": str(job.get("skill_uri") or ""),
                "artifact_quality": job.get("artifact_quality"),
                "output_sha256": {
                    path.relative_to(temporary / "output").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((temporary / "output").rglob("*"))
                    if path.is_file()
                },
            }
            _atomic_json(temporary / "meta.json", metadata)
            os.replace(temporary, destination)
        metadata = json.loads((destination / "meta.json").read_text(encoding="utf-8"))
        metadata["path"] = destination.relative_to(self.project_root).as_posix()
        return metadata

    def _archive_reviewed_output(self, job: dict[str, Any], submission: dict[str, Any] | None) -> None:
        destination = self._job_dir(job["job_id"]) / "rounds" / f"round-{int(job['current_round'])}"
        reviewed = destination / "reviewed_output"
        if not reviewed.exists():
            temporary = destination / "reviewed_output.tmp"
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(self.workspace(job), temporary)
            os.replace(temporary, reviewed)
        if submission is None:
            return
        _atomic_json(destination / "checkpoint_submission.json", submission)
        _atomic_json(
            self._job_dir(job["job_id"]) / "checkpoints" / "history" / f"round-{int(job['current_round'])}.json",
            submission,
        )

    def finish_materialized_round(self, job_id: str) -> dict[str, Any]:
        """Freeze Compile output, then wait for review or permit automatic continuation."""
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") in TERMINAL_JOB_STATES | {"waiting_human"}:
                return job
            if not job.get("materialized"):
                raise ValueError("本轮产物尚未通过结构校验")
            if int(job.get("orchestration_version") or 0) < 2:
                return self.update_job(job_id, status="succeeded", finished_at=utc_now())
            metadata = self._archive_round(job)
            history = [row for row in job.get("rounds_history") or [] if row.get("round") != metadata["round"]]
            history.append(metadata)
            if job.get("human_checkpoints_effective"):
                review = self._read_review(job)
                pending = {
                    "id": f"{job_id}-round-{job['current_round']}-review",
                    "round": job["current_round"],
                    "checkpoint": "after_compile",
                    "title": f"第 {job['current_round']} 轮 Evidence 复核与补充",
                    "intro": "检查本轮 Skill Candidate 和 Test Dataset；补充 Evidence 或明确标记暂时无法确认。",
                    "questions": [self._question_row(question) for question in review["questions"]],
                    "allow_stop": True,
                    "can_continue": int(job["current_round"]) < int(job["max_rounds"]),
                }
                quality = dict(job.get("artifact_quality") or {})
                quality["can_submit"] = False
                return self.update_job(
                    job_id,
                    rounds_history=history,
                    status="waiting_human",
                    pending_checkpoint=pending,
                    round_completed_at=metadata["completed_at"],
                    finished_at="",
                    artifact_quality=quality,
                    phase={"step1": "done", "step2": "done", "step3": "active"},
                )
            if int(job["current_round"]) >= int(job["max_rounds"]):
                quality = dict(job.get("artifact_quality") or {})
                quality["can_submit"] = True
                return self.update_job(
                    job_id,
                    rounds_history=history,
                    status="succeeded",
                    finished_at=utc_now(),
                    round_completed_at=metadata["completed_at"],
                    artifact_quality=quality,
                    phase={"step1": "done", "step2": "done", "step3": "done"},
                )
            return self.update_job(job_id, rounds_history=history, round_completed_at=metadata["completed_at"])

    def _validate_submission(
        self,
        job: dict[str, Any],
        checkpoint_id: str,
        answers: Any,
        unresolved: Any,
        references: Any,
        feedback: Any,
        request_id: Any,
        submitted_by: Any,
        attachments: Any,
    ) -> dict[str, Any]:
        pending = job.get("pending_checkpoint") or {}
        if job.get("status") != "waiting_human" or not pending:
            raise ValueError("该挖掘任务当前没有待填写的 Evidence 补充")
        if checkpoint_id != pending.get("id"):
            raise ValueError("复核问题已过期，请刷新任务详情")
        questions = {question["qid"]: question for question in pending.get("questions") or []}
        if not isinstance(answers, dict) or not isinstance(unresolved, list) or not isinstance(references, dict):
            raise ValueError("答案、未解决问题或来源引用格式无效")
        if any(
            not isinstance(key, str) or key not in questions or not isinstance(value, str)
            for key, value in answers.items()
        ):
            raise ValueError("答案必须由本轮问题 ID 和文本组成")
        if any(not isinstance(key, str) or key not in questions for key in unresolved):
            raise ValueError("未解决问题必须引用本轮问题 ID")
        normalized_answers = {key: value.strip() for key, value in answers.items() if value.strip()}
        normalized_unresolved = list(dict.fromkeys(unresolved))
        if set(normalized_answers) & set(normalized_unresolved):
            raise ValueError("同一问题不能同时回答并标记为未解决")
        for key, question in questions.items():
            if question.get("required") and key not in normalized_answers and key not in normalized_unresolved:
                raise ValueError(f"请回答必填问题或明确标记暂时无法确认：{key}")
        normalized_refs: dict[str, list[str]] = {}
        forbidden = [str(job.get("output_uri") or ""), str(job.get("round_context_uri") or "")]
        forbidden.extend(str(row.get("output_uri") or "") for row in job.get("rounds_history") or [])
        forbidden.extend(str(row.get("round_context_uri") or "") for row in job.get("rounds_history") or [])
        for key, values in references.items():
            if key not in questions or not isinstance(values, list) or len(values) > 20:
                raise ValueError("来源引用必须由本轮问题 ID 和最多 20 个 Viking URI 组成")
            if any(not isinstance(value, str) for value in values):
                raise ValueError("来源引用必须是 Viking URI 文本")
            normalized_refs[key] = list(dict.fromkeys(validate_resource_uri(value) for value in values))
            if any(
                root and (uri == root or uri.startswith(root + "/"))
                for uri in normalized_refs[key]
                for root in forbidden
            ):
                raise ValueError("上一轮产物和复核意见不能作为 Evidence 来源")
        if not isinstance(feedback, str) or len(feedback) > 20_000:
            raise ValueError("复核意见必须是最多 20000 字符的文本")
        if sum(len(value) for value in normalized_answers.values()) + len(feedback) > MAX_SUPPLEMENT_CHARACTERS:
            raise ValueError("本轮补充 Evidence 文本超过 100000 字符")
        if not isinstance(request_id, str) or len(request_id) > 200 or not isinstance(submitted_by, str):
            raise ValueError("提交标识或提交人格式无效")
        pending_attachments = list(job.get("pending_attachments") or [])
        selected = pending_attachments if attachments is None else attachments
        if (
            not isinstance(selected, list)
            or len(selected) > 20
            or any(not isinstance(row, dict) or row not in pending_attachments for row in selected)
        ):
            raise ValueError("补充文档必须是本轮已上传的附件")
        content = {
            "checkpoint_id": checkpoint_id,
            "answers": normalized_answers,
            "unresolved": normalized_unresolved,
            "references": normalized_refs,
            "feedback": feedback.strip(),
            "attachments": selected,
            "submitted_by": submitted_by,
            "round": job["current_round"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            **content,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "submitted_at": utc_now(),
            "checkpoint": pending["checkpoint"],
            "title": pending["title"],
            "intro": pending["intro"],
            "questions": pending["questions"],
            "source": "checkpoint",
            "reference_snapshots": list(job.get("pending_reference_snapshots") or []),
        }

    def _duplicate_submission(
        self,
        job: dict[str, Any],
        checkpoint_id: str,
        request_id: str,
        action: str,
        answers: Any,
        unresolved: Any,
        references: Any,
        feedback: Any,
        submitted_by: Any,
        attachments: Any,
    ) -> bool:
        last = job.get("last_checkpoint_submission") or {}
        if last.get("checkpoint_id") != checkpoint_id:
            if request_id and request_id == last.get("request_id"):
                raise ValueError("提交标识已经用于另一轮复核")
            return False
        if request_id and request_id != last.get("request_id"):
            return False
        if last.get("action") != action:
            raise ValueError("该轮复核已提交，请刷新任务详情")
        previous_job = {
            **job,
            "status": "waiting_human",
            "current_round": last["round"],
            "pending_checkpoint": {"id": checkpoint_id, **last},
            "pending_attachments": last.get("attachments") or [],
        }
        candidate = self._validate_submission(
            previous_job,
            checkpoint_id,
            answers,
            unresolved,
            references,
            feedback,
            request_id,
            submitted_by,
            attachments,
        )
        if candidate["fingerprint"] != last.get("fingerprint"):
            raise ValueError("该轮复核已提交，重复请求内容不一致")
        return True

    def _prepare_next_round(self, job: dict[str, Any], submission: dict[str, Any] | None = None) -> dict[str, Any]:
        round_number = int(job["current_round"]) + 1
        if round_number > int(job["max_rounds"]):
            raise ValueError("已达到挖掘轮数上限，请结束挖掘并保留最终产物")
        active = sum(
            row.get("engine") == "ov_compile"
            and row.get("status") in ACTIVE_JOB_STATES
            and row.get("job_id") != job["job_id"]
            for row in self._iter_jobs_unlocked()
        )
        if active >= MAX_PARALLEL_JOBS:
            raise RuntimeError(f"同时最多运行 {MAX_PARALLEL_JOBS} 个 OV Compile 挖掘任务")
        self._archive_round(job)
        if submission is not None:
            submission = {**submission, "action": "continue"}
        self._archive_reviewed_output(job, submission)
        run_root = f"{RUNS_ROOT}/{job['job_id']}"
        history = list(job.get("checkpoint_history") or [])
        supplements = list(job.get("supplements") or [])
        if submission is not None:
            submission = {**submission, "action": "continue"}
            history.append(submission)
            supplements.append(submission)
        round_record = next(row for row in job["rounds_history"] if row["round"] == job["current_round"])
        reset = {
            "status": "preparing",
            "current_round": round_number,
            "previous_round": round_record,
            "source_snapshot": f"{run_root}/rounds/round-{round_number}/source",
            "output_uri": f"{run_root}/rounds/round-{round_number}/output",
            "round_context_uri": f"{run_root}/rounds/round-{round_number}/context",
            "checkpoint_history": history,
            "supplements": supplements,
            "pending_checkpoint": None,
            "pending_attachments": [],
            "pending_reference_snapshots": [],
            "last_checkpoint_submission": submission or {},
            "submission_idempotency_key": f"te-sm-{job['job_id']}-round-{round_number}",
            "ov_task_id": "",
            "ov_status": "",
            "ov_stage": "",
            "started_at": "",
            "round_started_at": "",
            "finished_at": "",
            "error": "",
            "stop_reason": "",
            "materialized": False,
            "artifact_quality": None,
            "round_completed_at": "",
            "compile_request": {},
            "input_manifest": "",
            "setup_timings": {},
            "submission_retry_count": 0,
            "submission_attempts": 0,
            "submission_next_retry_at": "",
            "submission_started_at": "",
            "submission_error": "",
            "submission_finished_at": "",
            "submission_seconds": None,
            "materialize_attempts": 0,
            "materialize_seconds": None,
            "materialize_started_at": "",
            "materialize_finished_at": "",
            "ov_cancel_requested_at": "",
            "ov_cancel_reason": "",
            "ov_cancel_sent_at": "",
            "ov_cancel_error": "",
            "ov_last_synced_at": "",
            "ov_last_sync_attempt_at": "",
            "ov_sync_error": "",
            "ov_sync_failures": 0,
            "ov_next_retry_at": "",
            "ov_stage_history": [],
            "ov_event_timeline": [],
            "ov_task_created_at": "",
            "ov_task_started_at": "",
            "ov_task_updated_at": "",
            "ov_processing_seconds": None,
            "ov_pipeline": {},
            "remote_warnings": [],
            "compiler_sha256": "",
            "compiler_source_sha256": "",
            "compiler_uri": "",
            "skill_uri": SKILL_URI,
            "compiler_path": "",
            "compile_observed_seconds": None,
            "phase": {"step1": "active", "step2": "idle", "step3": "idle"},
            "workspace_reset_pending": True,
        }
        self.update_job(job["job_id"], **reset)
        return self.get_job(job["job_id"])

    def prepare_next_round(
        self,
        job_id: str,
        checkpoint_id: str,
        answers: dict[str, str],
        unresolved: list[str],
        references: dict[str, list[str]],
        feedback: str = "",
        request_id: str = "",
        *,
        submitted_by: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if self._duplicate_submission(
                job,
                checkpoint_id,
                request_id,
                "continue",
                answers,
                unresolved,
                references,
                feedback,
                submitted_by,
                attachments,
            ):
                return job
            submission = self._validate_submission(
                job,
                checkpoint_id,
                answers,
                unresolved,
                references,
                feedback,
                request_id,
                submitted_by,
                attachments,
            )
            return self._prepare_next_round(job, submission)

    def prepare_automatic_next_round(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if (
                job.get("human_checkpoints_effective")
                or not job.get("materialized")
                or job.get("status") in TERMINAL_JOB_STATES
            ):
                raise ValueError("该任务当前不能自动开始下一轮")
            return self._prepare_next_round(job)

    def finish_job(
        self,
        job_id: str,
        checkpoint_id: str,
        *,
        answers: dict[str, str] | None = None,
        unresolved: list[str] | None = None,
        references: dict[str, list[str]] | None = None,
        feedback: str = "",
        request_id: str = "",
        submitted_by: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            answers = {} if answers is None else answers
            unresolved = [] if unresolved is None else unresolved
            references = {} if references is None else references
            if self._duplicate_submission(
                job,
                checkpoint_id,
                request_id,
                "finish",
                answers,
                unresolved,
                references,
                feedback,
                submitted_by,
                attachments,
            ):
                return job
            submission = self._validate_submission(
                job,
                checkpoint_id,
                answers,
                unresolved,
                references,
                feedback,
                request_id,
                submitted_by,
                attachments,
            )
            submission["action"] = "finish"
            self._archive_reviewed_output(job, submission)
            unresolved_qids = list(
                dict.fromkeys(
                    [
                        *submission["unresolved"],
                        *(
                            question["qid"]
                            for question in submission["questions"]
                            if question["qid"] not in submission["answers"]
                        ),
                    ]
                )
            )
            quality = dict(job.get("artifact_quality") or {})
            warnings = list(quality.get("warnings") or [])
            if unresolved_qids:
                warnings.append("结束挖掘时仍有未解决的 Evidence 缺口：" + "、".join(unresolved_qids))
            if (
                submission["answers"]
                or submission["attachments"]
                or submission["feedback"]
                or any(submission["references"].values())
                or submission["reference_snapshots"]
            ):
                warnings.append("最终复核新增的 Evidence 或意见尚未经过下一轮 Compile；请在 Candidate Review 中确认。")
            if warnings:
                quality.update(level="caution", label="需复核", warnings=list(dict.fromkeys(warnings)))
            quality["can_submit"] = True
            return self.update_job(
                job_id,
                status="succeeded",
                finished_at=utc_now(),
                pending_checkpoint=None,
                pending_attachments=[],
                pending_reference_snapshots=[],
                checkpoint_history=[*(job.get("checkpoint_history") or []), submission],
                supplements=[*(job.get("supplements") or []), submission],
                last_checkpoint_submission=submission,
                unresolved_questions=unresolved_qids,
                artifact_quality=quality,
                phase={"step1": "done", "step2": "done", "step3": "done"},
            )

    def stop_waiting(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") != "waiting_human":
                raise ValueError("该任务当前没有等待人工复核")
            return self.update_job(
                job_id,
                status="stopped",
                pending_checkpoint=None,
                finished_at=utc_now(),
                stop_reason="用户停止了等待人工复核的挖掘任务",
            )

    def _public_checkpoint(self, job: dict[str, Any]) -> dict[str, Any] | None:
        pending = job.get("pending_checkpoint")
        if not isinstance(pending, dict):
            return None
        return {
            **pending,
            "attachments": [self._public_attachment(row) for row in job.get("pending_attachments") or []],
        }

    @staticmethod
    def _public_attachment(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "local_path"}

    def _public_submission(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "attachments": [self._public_attachment(item) for item in row.get("attachments") or []],
            "reference_snapshots": [self._public_attachment(item) for item in row.get("reference_snapshots") or []],
        }

    def _public_gaps(self, job: dict[str, Any]) -> dict[str, Any] | None:
        if not job.get("materialized"):
            return None
        try:
            questions = [self._question_row(row) for row in self._read_review(job)["questions"]]
        except ValueError:
            return None
        return {"total": len(questions), "questions": questions}

    def _public_rounds(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in job.get("rounds_history") or []:
            root = self._job_dir(job["job_id"]) / "rounds" / f"round-{int(row['round'])}"
            result.append(
                {
                    **row,
                    "artifacts": self._artifact_rows(root / "output"),
                    "reviewed_artifacts": self._artifact_rows(root / "reviewed_output"),
                }
            )
        return result

    def _artifact_rows(self, workspace: Path) -> list[dict[str, Any]]:
        kinds = {
            "SKILL.md": "skill",
            "EVALUATION.md": "evaluation",
            "BENCHMARK.md": "benchmark",
            "benchmark.json": "benchmark",
        }
        result: list[dict[str, Any]] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            kind = kinds.get(path.name)
            if kind is None and "semantic_reports" in path.parts:
                kind = "semantic"
            if kind is not None:
                result.append(
                    {
                        "name": path.name,
                        "kind": kind,
                        "path": path.relative_to(self.project_root).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "skill_name": path.parent.name if "compiled_skill" in path.parts else "",
                    }
                )
        return result

    def public_job(self, job: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        hidden = {"workspace", "input_manifest", "compile_request", "workspace_reset_pending", "compiler_path"}
        result = {key: value for key, value in job.items() if key not in hidden}
        for key in ("pending_attachments", "pending_reference_snapshots"):
            result[key] = [self._public_attachment(row) for row in job.get(key) or []]
        for key in ("checkpoint_history", "supplements"):
            result[key] = [self._public_submission(row) for row in job.get(key) or []]
        result["last_checkpoint_submission"] = self._public_submission(job.get("last_checkpoint_submission") or {})
        result["pending_checkpoint"] = self._public_checkpoint(job)
        result["remote_state_stale"] = bool(
            job.get("engine") == "ov_compile" and job.get("status") in ACTIVE_JOB_STATES and job.get("ov_sync_error")
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
                "rounds": self._public_rounds(job),
                "logs": logs,
                "skills": skills,
                "pending_checkpoint": self._public_checkpoint(job),
                "knowledge_gaps": self._public_gaps(job),
                "knowledge_supplements": [self._public_submission(row) for row in job.get("checkpoint_history") or []],
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
            "waiting": sum(job.get("status") == "waiting_human" for job in jobs),
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
