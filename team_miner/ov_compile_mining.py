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
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from team_replay.datasets.schema import document_skill_ids
from team_skills.library.frontmatter import parse_skill_md_text

from . import benchmark_format
from .compile_strategy import (
    compiler_execution_guidance,
    mining_policy_block,
    multi_compiler_execution_guidance,
    register_owned_compiler_policy,
)
from .dataset_view import dataset_markdown
from .output_language import OUTPUT_LANGUAGE_GUIDANCE

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
MAX_SKILL_FRONTMATTER_CHARACTERS = 64 * 1024
RECOVERABLE_IDENTITY_ERROR = "Prior candidate name cannot silently change its canonical workflow; use explicit reuse"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skill_metadata(path: Path) -> dict | None:
    """Read bounded frontmatter and verify all UTF-8 bytes without retaining the body."""
    try:
        with path.open("r", encoding="utf-8") as stream:
            first = stream.readline(MAX_SKILL_FRONTMATTER_CHARACTERS + 1)
            if not re.fullmatch(r"---[ \t]*\n", first):
                return None
            header, size = [first], len(first)
            while True:
                line = stream.readline(MAX_SKILL_FRONTMATTER_CHARACTERS - size + 1)
                size += len(line)
                if size > MAX_SKILL_FRONTMATTER_CHARACTERS:
                    raise ValueError("SKILL.md frontmatter 不能超过 64K 字符；正文没有此限制")
                if not line:
                    return None
                header.append(line)
                if re.fullmatch(r"---[ \t]*(?:\n|$)", line):
                    break
            metadata = parse_skill_md_text("".join(header))
            # A valid header alone cannot hide a broken or incomplete UTF-8
            # sequence later in the body. This read is bounded independently
            # of the total Skill length.
            while stream.read(64 * 1024):
                pass
            return metadata
    except OSError:
        return None
    except UnicodeError as exc:
        raise ValueError("SKILL.md 必须为完整有效的 UTF-8 文本") from exc

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

SKILL_BODY += "\n## Output language\n\n" + OUTPUT_LANGUAGE_GUIDANCE + "\n"

LEGACY_SKILL_BODY = SKILL_BODY + compiler_execution_guidance()
SKILL_BODY = (
    SKILL_BODY.replace(
        "into one evidence-grounded Skill Candidate and its Test Dataset.",
        "into an evidence-grounded Skill Candidate collection with independent Test Datasets.",
    )
    .replace(
        "produce one\nreviewable Skill Candidate.",
        "plan and produce an evidence-grounded\nSkill Candidate collection.",
    )
    .replace(
        "- Produce exactly one Skill Candidate and use a lowercase kebab-case skill name.",
        "- Plan independently executable methods before writing. TE partitions the collection into "
        "bounded planning assignments; the number of supported methods may increase the number of assignments. "
        "A per-assignment output limit is never a reason to discard a supported method. "
        "An executable method performs a reusable decision, calculation or procedure with clear inputs "
        "and outputs; looking up a name, code, version or description alone is factual information. "
        "Missing support for one operation restricts that operation, not all methods from its source file. "
        "Keep each candidate's Skill, evaluation and Checklist within the same planned task boundary. "
        "Use lowercase kebab-case names. Each candidate has its own complete four-file bundle.",
    )
    .replace(
        "Write the following files below the requested output directory:",
        "Write shared analysis and a capability plan below the requested output directory. "
        "Repeat the four compiled_skill files for EACH planned Skill Candidate:\n\n"
        "- `analysis/capability-plan.json`: schema_version `skill-miner.plan.v1`, skills and deferred arrays. "
        "Each skills entry has name, task, trigger, inputs, outputs, boundary, rationale and source_refs. "
        "Each deferred entry has reason and source_refs. Explain why methods are separate or merged. "
        "If no executable method is supported, emit skills=[] and explain the disposition of the content. "
        "Pure facts do not require a fabricated Evidence gap or a request for human supplementation; "
        "do not emit a compiled_skill bundle.\n",
    )
)
SKILL_BODY += multi_compiler_execution_guidance()
# Exact owned defaults remain recognizable for fresh auxiliary stages on an
# already frozen round. Arbitrary custom text is never inferred or stripped.
register_owned_compiler_policy(LEGACY_SKILL_BODY)
register_owned_compiler_policy(SKILL_BODY)
SKILL_BODY += mining_policy_block()
register_owned_compiler_policy(SKILL_BODY)
MAX_CANDIDATES = 8
PLAN_SCHEMA_VERSION = "skill-miner.plan.v1"


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
        skip_empty_checkpoints: bool = False,
        consolidate_reviewed_candidates: bool = False,
        candidate_contract_version: int = 2,
        compiler_body: str | None = None,
        mining_mode: str = "auto",
        mining_direction: str = "",
        target_skill_count: int | None = None,
    ) -> dict[str, Any]:
        from .mining_intent import normalize_intent
        intent = normalize_intent(mining_mode, mining_direction, target_skill_count)
        if intent["active"] and candidate_contract_version != 2:
            raise ValueError("挖掘方向与数量模式需要当前多候选编译契约，请恢复默认编译 Skill")
        if compiler_body is not None and (not isinstance(compiler_body, str) or not compiler_body.strip()):
            raise ValueError("编译 Skill 必须为非空文本")
        if isinstance(candidate_contract_version, bool) or candidate_contract_version not in (1, 2):
            raise ValueError("Skill Candidate 编译契约版本必须为 1 或 2")
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
            if source_snapshot_uri == f"{RUNS_ROOT}/pending/source":
                source_snapshot_uri = f"{RUNS_ROOT}/{job_id}/rounds/round-1/source"
            if output_uri == f"{RUNS_ROOT}/pending/output":
                output_uri = f"{RUNS_ROOT}/{job_id}/rounds/round-1/output"
            normalized_rounds = max_rounds
            job = {
                "job_id": job_id,
                "name": str(name or f"{source_uri.rsplit('/', 1)[-1]} 挖掘任务")[:120],
                "mining_intent": intent,
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
                "staged_execution_protocol": "te-mining-optimized.v1" if candidate_contract_version == 2 else "",
                "staged_efficiency_protocol": "te-mining-efficient.v2" if candidate_contract_version == 2 else "",
                "staged_workflow_protocol": "te-direct-workflow-planning.v1" if candidate_contract_version == 2 else "",
                "staged_workflow_partition_protocol": (
                    "te-workflow-partition.v2" if candidate_contract_version == 2 else ""
                ),
                "staged_source_scope_protocol": "te-source-scope.v3" if candidate_contract_version == 2 else "",
                "staged_selection_recovery_protocol": (
                    "te-selection-recovery.v2" if candidate_contract_version == 2 else ""
                ),
                "staged_source_fragment_protocol": "te-source-fragments.v1" if candidate_contract_version == 2 else "",
                "staged_source_input_clarity_protocol": (
                    "te-source-input-clarity.v1" if candidate_contract_version == 2 else ""
                ),
                "staged_planning_alias_protocol": "te-planning-aliases.v1" if candidate_contract_version == 2 else "",
                "staged_case_group_protocol": "te-case-groups.v1" if candidate_contract_version == 2 else "",
                "staged_source_unit_protocol": "te-exact-source-units.v1" if candidate_contract_version == 2 else "",
                "staged_skill_body_protocol": "te-skill-body-patch.v1" if candidate_contract_version == 2 else "",
                "staged_compact_planning_protocol": "te-compact-planning.v1" if candidate_contract_version == 2 else "",
                "staged_quote_alias_protocol": (
                    "te-evidence-quote-aliases.v1" if candidate_contract_version == 2 else ""
                ),
                "staged_identity_partition_protocol": (
                    ("te-candidate-identity-partition.v3" if consolidate_reviewed_candidates
                     else "te-candidate-identity-partition.v2") if candidate_contract_version == 2 else ""
                ),
                "staged_uncertainty_scope_protocol": (
                    "te-uncertain-context-units.v1" if candidate_contract_version == 2 else ""
                ),
                "candidate_contract_version": candidate_contract_version,
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
                "empty_review_policy": "te-empty-review-skip.v1" if skip_empty_checkpoints else "",
                "skipped_checkpoints": [],
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
            from .mining_completion import POLICY
            from .native_compiler import PROTOCOL as NATIVE_PROTOCOL
            if compiler_body and f"completion_policy: {POLICY}" in compiler_body.splitlines():
                job["completion_policy"] = POLICY
                job["benchmark_required"] = False
            if compiler_body and NATIVE_PROTOCOL in compiler_body:
                job["execution_protocol"] = NATIVE_PROTOCOL
                job["stable_output_uri"] = f"{RUNS_ROOT}/{job_id}/output"
                job["output_uri"] = job["stable_output_uri"]
                job["native_compile_history"] = []
                # These flags describe the old host pipeline, not native execution.
                for key in list(job):
                    if key.startswith("staged_"):
                        job.pop(key)
            job_dir = self._job_dir(job_id)
            (job_dir / "workspace" / "compiled_skill").mkdir(parents=True, exist_ok=False)
            (job_dir / "workspace" / "semantic_reports").mkdir(parents=True, exist_ok=True)
            job["workspace"] = (job_dir / "workspace").relative_to(self.project_root).as_posix()
            if compiler_body is not None:
                (job_dir / "compiler-skill.md").write_text(compiler_body, encoding="utf-8")
                from .native_compiler import freeze_assets
                freeze_assets(job_dir, compiler_body)
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
            if row.get("status") in {"stopping", "stopped"} or row.get("ov_cancel_reason"):
                # A late worker result may record acceptance, but cannot undo a
                # stop committed while it was reading or awaiting the remote ACK.
                uncertain_stop = (changes.get("status") == "interrupted" and row.get("ov_cancel_sent_at")
                                  and (changes.get("remote_terminal_unconfirmed")
                                       or row.get("remote_terminal_unconfirmed")))
                if changes.get("status") not in {"stopping", "stopped"} and not uncertain_stop:
                    changes = {
                        key: value for key, value in changes.items()
                        if key not in {"status", "finished_at", "error", "stop_reason"}
                    }
                for key in ("ov_cancel_reason", "ov_cancel_requested_at", "stop_reason", "ov_task_id"):
                    if row.get(key) and key in changes and not changes[key]:
                        changes.pop(key)
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
            if row.get("status") in ACTIVE_JOB_STATES | {"waiting_human"} or row.get("remote_terminal_unconfirmed"):
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

    def _output_target(self, job_id: str, relative_path: str) -> Path:
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
        return target

    def write_output(self, job_id: str, relative_path: str, content: str) -> Path:
        target = self._output_target(job_id, relative_path)
        target.write_text(content, encoding="utf-8")
        return target

    def write_output_stream(self, job_id: str, relative_path: str, chunks: Iterable[str]) -> Path:
        """Commit a complete validated text stream atomically, preserving any previous artifact on error."""
        target = self._output_target(job_id, relative_path)
        temporary = target.with_name(f".te-stream-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, str):
                        raise ValueError("OV Compile 产物流必须包含 UTF-8 文本")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def finalize_materialized(self, job_id: str, *, after_edit: bool = False, render_skill: str = "") -> dict[str, Any]:
        job = self.get_job(job_id)
        from .mining_completion import completion_only, finalize_inventory
        if completion_only(job):
            return finalize_inventory(self, job)
        workspace = self.workspace(job)
        for name in ("evidence-map.md", "gaps.md"):
            report = workspace / "semantic_reports" / name
            if not report.is_file() or not report.read_text(encoding="utf-8").strip():
                raise ValueError(f"OV Compile 未生成 analysis/{name}")
        skill_files = sorted((workspace / "compiled_skill").glob("*/SKILL.md"))
        plan = self._read_capability_plan(job) if int(job.get("candidate_contract_version") or 1) >= 2 else None
        if plan is None and len(skill_files) != 1:
            raise ValueError(f"OV Compile 应生成一个 Skill Candidate，实际找到 {len(skill_files)} 个")
        planned_names = (
            {row["name"] for row in plan["skills"]} if plan is not None else {p.parent.name for p in skill_files}
        )
        actual_names = {p.name for p in (workspace / "compiled_skill").iterdir() if p.is_dir()}
        if plan is not None and not actual_names <= planned_names:
            raise ValueError("Skill Candidate 产物与能力拆分计划不一致")
        candidate_quality = {}
        question_count = 0
        valid_count = 0
        warnings = [item.strip() for item in job.get("remote_warnings") or [] if isinstance(item, str) and item.strip()]
        for name in sorted(planned_names):
            skill_dir = workspace / "compiled_skill" / name
            try:
                count = self._validate_candidate(
                    job, skill_dir, render_view=plan is not None and (not after_edit or name == render_skill)
                )
                valid_count += 1
                question_count += count
                errors = []
            except ValueError as exc:
                if plan is None:
                    raise
                count = 0
                errors = [str(exc)]
                warnings.append(f"{name}：{exc}")
            candidate_quality[name] = {
                "has_skill": (skill_dir / "SKILL.md").is_file(),
                "level": "incomplete" if errors else "ready",
                "label": "产物不完整" if errors else "可提交",
                "confidence": "候选级",
                "summary": f"Test Dataset · {count} 条" if not errors else errors[0],
                "warnings": errors,
                "artifacts": [],
                "can_submit": not errors,
            }
        if planned_names and not valid_count and not after_edit:
            raise ValueError("所有 Skill Candidate 均未通过产物校验：" + "；".join(warnings))
        if not planned_names:
            warnings.append("Evidence 尚不足以形成可独立执行的 Skill；请查看能力拆分计划。")
        if int(job.get("orchestration_version") or 0) >= 2:
            review = self._read_review(job)
            warnings.extend(question["question"] for question in review["questions"])
        if after_edit:
            # Keep human review warnings while replacing stale per-candidate errors.
            previous_warnings = (job.get("artifact_quality") or {}).get("warnings") or []
            warnings.extend(
                value
                for value in previous_warnings
                if isinstance(value, str) and not any(value.startswith(name + "：") for name in planned_names)
            )
        quality = {
            "has_skill": bool(valid_count),
            "candidate_count": len(planned_names),
            "level": "incomplete" if planned_names and not valid_count else "caution" if warnings else "ready",
            "label": "需复核" if warnings else "可提交",
            "confidence": "候选级",
            "summary": (
                f"规划 {len(planned_names)} 个 Skill Candidate，其中 {valid_count} 个通过产物校验，"
                f"共 {question_count} 条案例。"
            ),
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
            "can_submit": bool(valid_count)
            and (
                (after_edit and job.get("status") == "succeeded")
                or int(job.get("orchestration_version") or 0) < 2
                or (
                    not bool(job.get("human_checkpoints_effective"))
                    and int(job.get("current_round") or 1) >= int(job.get("max_rounds") or 1)
                )
            ),
        }
        if not planned_names:
            quality["artifacts"] = []
            quality["label"] = "未形成候选"
        return self.update_job(
            job_id,
            materialized=bool(valid_count) or not planned_names,
            artifact_quality=quality,
            capability_plan=plan,
            candidate_count=len(planned_names),
            candidate_quality=candidate_quality,
            valid_candidate_count=valid_count,
        )

    def refresh_edited_quality(self, job_id: str, relative_path: str) -> dict[str, Any]:
        """Refresh selected candidates after edits without changing the review gate."""
        job = self.get_job(job_id)
        if int(job.get("candidate_contract_version") or 1) < 2:
            return job
        parts = PurePosixPath(relative_path).parts
        # Repairs to a candidate's prerequisites may reach the deterministic
        # view renderer for the first time. Direct edits to that view stay
        # validation-only so an inconsistent human edit is never overwritten.
        render_skill = parts[-2] if parts[-1] in {"SKILL.md", "EVALUATION.md", "benchmark.json"} else ""
        try:
            return self.finalize_materialized(job_id, after_edit=True, render_skill=render_skill)
        except ValueError as exc:
            quality = dict(job.get("artifact_quality") or {})
            quality.update(
                level="incomplete",
                label="需修复",
                has_skill=False,
                can_submit=False,
                summary=str(exc),
                warnings=[str(exc)],
            )
            candidates = {
                name: {**value, "can_submit": False} for name, value in (job.get("candidate_quality") or {}).items()
            }
            return self.update_job(job_id, artifact_quality=quality, candidate_quality=candidates, materialized=False)

    def _validate_candidate(self, job: dict[str, Any], skill_dir: Path, *, render_view: bool = False) -> int:
        parsed = _skill_metadata(skill_dir / "SKILL.md")
        if parsed is None:
            raise ValueError("SKILL.md frontmatter 无效：请检查 YAML 语法和必填 name / description 字段")
        if str(parsed.get("name") or "") != skill_dir.name:
            raise ValueError("SKILL.md frontmatter 的 name 必须与目录名一致")
        evaluation = skill_dir / "EVALUATION.md"
        if not evaluation.is_file() or not evaluation.stat().st_size:
            raise ValueError("OV Compile 未生成 EVALUATION.md")

        benchmark_path = skill_dir / "benchmark.json"
        if not benchmark_path.is_file():
            raise ValueError("OV Compile 未生成 benchmark.json")
        document, errors = benchmark_format.read_document(benchmark_path)
        if document is not None:
            errors.extend(benchmark_format.validate_document(document, expected_skill_name=skill_dir.name))
        if errors:
            raise ValueError("Test Dataset 格式不合法：" + "；".join(dict.fromkeys(errors)))
        if int(job.get("candidate_contract_version") or 1) >= 2:
            if document_skill_ids(document or {}) != [skill_dir.name] or any(
                case.get("skill_ids") != [skill_dir.name] for case in (document or {}).get("cases") or []
            ):
                raise ValueError("独立 Test Dataset 只能引用当前 Skill Candidate")
        case_count = len((document or {}).get("cases") or [])
        if not 12 <= case_count <= 24:
            raise ValueError(f"Test Dataset 应有 12–24 条案例，实际为 {case_count} 条")
        for case in (document or {}).get("cases") or []:
            citations = (case.get("provenance") or {}).get("session_ids") or []
            if not citations or any(not isinstance(uri, str) or not uri.startswith("viking://") for uri in citations):
                raise ValueError("每条 Test Dataset 案例必须包含非空的来源 Viking URI")
            for uri in citations:
                if int(job.get("candidate_contract_version") or 1) >= 2:
                    uri = validate_resource_uri(uri)
                if not any(
                    uri == prefix or str(uri).startswith(prefix + "/") for prefix in self._citation_prefixes(job)
                ):
                    raise ValueError("Test Dataset 引用不属于本任务知识源")
        benchmark_md = skill_dir / "BENCHMARK.md"
        if not benchmark_md.is_file() or not benchmark_md.read_text(encoding="utf-8").strip():
            raise ValueError("OV Compile 未生成完整的 BENCHMARK.md，禁止自动补齐必需产物")
        if int(job.get("candidate_contract_version") or 1) >= 2:
            if render_view:
                # The model must emit all required files. Normalize only this derived
                # human view after the canonical JSON has passed validation.
                benchmark_md.write_text(dataset_markdown(document), encoding="utf-8")
            else:
                view = benchmark_md.read_text(encoding="utf-8")
                for case in document["cases"]:
                    required = [case["case_id"], case["query"]] + [check["text"] for check in case["checks"]]
                    if any(value not in view for value in required):
                        raise ValueError("BENCHMARK.md 与 Test Dataset 不一致，请重新编译当前候选")
        return case_count

    def _read_capability_plan(self, job: dict[str, Any]) -> dict[str, Any]:
        path = self.workspace(job) / "semantic_reports" / "capability-plan.json"
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("OV Compile 未生成合法的 analysis/capability-plan.json") from exc
        if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise ValueError("能力拆分计划 schema_version 无效")
        skills, deferred = plan.get("skills"), plan.get("deferred")
        if not isinstance(skills, list) or not isinstance(deferred, list):
            raise ValueError("能力拆分计划必须包含 skills/deferred 数组")
        from .native_compiler import is_native
        if not is_native(job) and job.get("staged_protocol") != "te-staged-compile.v1" and len(skills) > MAX_CANDIDATES:
            raise ValueError("单次 Compile 的能力拆分计划最多包含 8 个候选")
        if not skills and not deferred and job.get("staged_protocol") != "te-staged-compile.v1":
            raise ValueError("零候选的能力拆分计划必须说明暂缓原因")
        names = set()
        for row in skills + deferred:
            if not isinstance(row, dict):
                raise ValueError("能力拆分计划条目无效")
            fields = ("name", "task", "trigger", "boundary", "rationale") if row in skills else ("reason",)
            if any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
                raise ValueError("能力拆分计划缺少任务边界或暂缓原因")
            refs = row.get("source_refs")
            if isinstance(refs, list):
                refs = [validate_resource_uri(uri) for uri in refs]
            if (
                not isinstance(refs, list)
                or not refs
                or any(
                    not isinstance(uri, str)
                    or not any(uri == root or uri.startswith(root + "/") for root in self._citation_prefixes(job))
                    for uri in refs
                )
            ):
                raise ValueError("能力拆分计划引用不属于本任务知识源")
        for row in skills:
            name = row["name"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 80 or name in names:
                raise ValueError("能力拆分计划候选名称无效或重复")
            names.add(name)
            for key in ("inputs", "outputs"):
                values = row.get(key)
                if (
                    not isinstance(values, list)
                    or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)
                ):
                    raise ValueError("能力拆分计划缺少独立任务输入输出")
        return plan

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
                "output_language_policy": job.get("output_language_policy") or {},
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
                "output_language_policy": job.get("output_language_policy") or {},
                "artifact_quality": job.get("artifact_quality"),
                "output_sha256": {
                    path.relative_to(temporary / "output").as_posix(): _file_sha256(path)
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
            from .mining_completion import complete, completion_only
            if completion_only(job):
                return complete(self, job)
            if not job.get("materialized"):
                raise ValueError("本轮产物尚未通过结构校验")
            if int(job.get("orchestration_version") or 0) < 2:
                return self.update_job(job_id, status="succeeded", finished_at=utc_now())
            metadata = self._archive_round(job)
            history = [row for row in job.get("rounds_history") or [] if row.get("round") != metadata["round"]]
            history.append(metadata)
            if job.get("human_checkpoints_effective"):
                review = self._read_review(job)
                if job.get("empty_review_policy") == "te-empty-review-skip.v1" and not review["questions"]:
                    # A declared empty review is not human approval or new Evidence.
                    # Keep a separate receipt; never fabricate a checkpoint submission.
                    record = {
                        "round": job["current_round"], "checkpoint": "after_compile",
                        "policy": "te-empty-review-skip.v1", "reason": "本轮无补证问题，自动跳过人工补证",
                        "question_count": 0, "human_approved": False, "new_evidence": False,
                        "review_sha256": _file_sha256(self.workspace(job) / "semantic_reports/review.json"),
                        "recorded_at": metadata["completed_at"],
                    }
                    if (record in (job.get("skipped_checkpoints") or [])
                            and job.get("round_completed_at") == metadata["completed_at"]):
                        return job
                    skipped = [row for row in job.get("skipped_checkpoints") or []
                               if row["round"] != job["current_round"]] + [record]
                    job = self.update_job(job_id, skipped_checkpoints=skipped, pending_checkpoint=None)
                else:
                    return self._wait_for_round_review(job, metadata, history, review)
            from .native_compiler import is_native
            no_native_work = is_native(job) and (
                not job.get("human_checkpoints_effective") or not self._read_review(job)["questions"]
            )
            if int(job["current_round"]) >= int(job["max_rounds"]) or no_native_work:
                quality = dict(job.get("artifact_quality") or {})
                quality["can_submit"] = bool(quality.get("has_skill"))
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

    def _wait_for_round_review(self, job: dict, metadata: dict, history: list, review: dict) -> dict:
        job_id = job["job_id"]
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
            "staged_protocol": "",
            "staged_state_path": "",
            "staged_progress": {},
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
            "capability_plan": None,
            "candidate_count": 0,
            "candidate_quality": {},
            "valid_candidate_count": 0,
            "workspace_reset_pending": True,
        }
        from .native_compiler import is_native
        if is_native(job):
            reset["output_uri"] = job["stable_output_uri"]
            reset["workspace_reset_pending"] = False
            reset["native_repair_instruction"] = ""
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
            from .mining_completion import completion_only, revision_submission_job
            submission_job = (revision_submission_job(job, checkpoint_id, feedback)
                              if completion_only(job) else job)
            submission = self._validate_submission(
                submission_job,
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
                not job.get("materialized")
                or job.get("status") in TERMINAL_JOB_STATES | {"waiting_human"}
            ):
                raise ValueError("该任务当前不能自动开始下一轮")
            if job.get("human_checkpoints_effective"):
                records = job.get("skipped_checkpoints") or []
                receipt = next((row for row in records if row.get("round") == job["current_round"]), {})
                review_path = self.workspace(job) / "semantic_reports/review.json"
                if (job.get("empty_review_policy") != "te-empty-review-skip.v1"
                        or receipt.get("policy") != "te-empty-review-skip.v1"
                        or receipt.get("question_count") != 0
                        or receipt.get("human_approved") is not False
                        or receipt.get("review_sha256") != _file_sha256(review_path)
                        or self._read_review(job)["questions"]):
                    raise ValueError("有补证问题或空检查点记录已变化，不能自动开始下一轮")
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
            quality["can_submit"] = bool(quality.get("has_skill"))
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
            relative = path.relative_to(workspace)
            parts = relative.parts
            is_reference = len(parts) >= 4 and parts[0] == "compiled_skill" and parts[2] == "references"
            if is_reference:
                if (
                    path.suffix.lower() not in {".md", ".json"}
                    or any(part.startswith(".") for part in parts)
                    or path.is_symlink()
                    or any((workspace / parent).is_symlink() for parent in relative.parents)
                ):
                    continue
                kind = "resource"
            else:
                kind = kinds.get(path.name)
            if kind is None and "semantic_reports" in path.parts:
                kind = "semantic"
            if kind is not None:
                display_title = ""
                if kind == "skill" and not path.is_symlink() and path.resolve().is_relative_to(workspace.resolve()):
                    from team_skills.library.frontmatter import split_skill_md

                    with path.open(encoding="utf-8", errors="replace") as stream:
                        content = stream.read(16384)
                    split = split_skill_md(content)
                    body = split[1] if split else content
                    first_line = body.strip().splitlines()[0] if body.strip() else ""
                    if first_line.startswith("# "):
                        display_title = first_line[2:].strip().rstrip("#").strip()[:200]
                result.append(
                    {
                        "name": path.name,
                        "kind": kind,
                        "path": path.relative_to(self.project_root).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "skill_name": parts[1] if len(parts) >= 3 and parts[0] == "compiled_skill" else "",
                        "display_title": display_title,
                    }
                )
        return result

    def public_job(self, job: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        hidden = {
            "workspace", "input_manifest", "compile_request", "workspace_reset_pending", "compiler_path",
            "staged_state_path", "native_allowed_source_uris",
        }
        result = {key: value for key, value in job.items() if key not in hidden}
        from .mining_completion import revision_checkpoint
        result["optional_revision"] = revision_checkpoint(job)
        from .artifact_recovery import ERROR_PREFIX, prepare_recovery

        result["can_resume"] = job.get("status") == "failed" and (
            job.get("error") == RECOVERABLE_IDENTITY_ERROR
        )
        result["can_retry_native"] = False
        result["native_retry_context"] = None
        from .native_compiler import is_native
        if is_native(job) and job.get("status") == "failed":
            from .native_recovery import prepare_recovery as prepare_native_recovery
            try:
                prepare_native_recovery(self._job_dir(job["job_id"]), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            from .native_recovery import prepare_explicit_retry
            try:
                result["native_retry_context"] = prepare_explicit_retry(self._job_dir(job["job_id"]), job)
                result["can_retry_native"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and str(job.get("error", "")).startswith(ERROR_PREFIX):
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        from .extract_recovery import ERROR_PREFIX as EXTRACT_ERROR_PREFIX
        from .extract_recovery import prepare_recovery as prepare_extract_recovery
        from .intent_recovery import ERROR as INTENT_ERROR
        from .intent_recovery import prepare_recovery as prepare_intent_recovery
        from .routing_recovery import ERROR as ROUTING_ERROR
        from .routing_recovery import prepare_recovery as prepare_routing_recovery

        if job.get("status") == "failed" and str(job.get("error", "")).startswith(EXTRACT_ERROR_PREFIX):
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_extract_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and job.get("error") == ROUTING_ERROR:
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_routing_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if job.get("status") == "failed" and job.get("error") == INTENT_ERROR:
            try:
                root = self._job_dir(job["job_id"]) / "round_inputs" / f"round-{job['current_round']}" / "staged"
                prepare_intent_recovery(root, json.loads((root / "state.json").read_text(encoding="utf-8")), job)
                result["can_resume"] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
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
        artifacts = self._artifact_rows(workspace)
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

    def resume_native_failure(self, job_id: str, remote_task: dict, *,
                              authorization: dict | None = None) -> dict[str, Any]:
        """Apply a bounded request-contract upgrade; accepted requests and retry counts stay immutable."""
        from .native_recovery import explicit_retry_matches, prepare_explicit_retry, prepare_recovery

        with self._lock:
            job = self.get_job(job_id)
            if authorization is not None and explicit_retry_matches(job, authorization):
                return job
            if (authorization is None and (job.get("native_resource_recovery") or job.get("native_contract_recoveries"))
                    and job.get("status") in ACTIVE_JOB_STATES):
                return job
            active = sum(row.get("status") in ACTIVE_JOB_STATES for row in self._iter_jobs_unlocked())
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError("运行并发已满，请稍后恢复")
            journal = (prepare_recovery(self._job_dir(job_id), job, apply=True, remote_task=remote_task)
                       if authorization is None else prepare_explicit_retry(self._job_dir(job_id), job,
                           apply=True, remote_task=remote_task, authorization=authorization))
            now = utc_now()
            upgrades = list(job.get("native_contract_recoveries") or [])
            if authorization is None:
                upgrades.append({key: journal[key] for key in ("protocol", "submission_key", "round", "task_id",
                                                              "guidance_sha256", "upgrade_index")})
            meta = journal if authorization is None else journal["retry"]
            reason = (journal["reason"] if authorization is None
                      else "管理员明确授权一次格式修复实测，追加独立尝试预算")
            history = list(job.get("recovery_history") or [])
            history.append({"round": job["current_round"], "error": job["error"],
                            "failed_at": job["finished_at"], "resumed_at": now,
                            "reason": reason, "recovery_protocol": journal["protocol"],
                            "guidance_sha256": meta["guidance_sha256"],
                            **({"explicit_retry": meta} if authorization is not None else {}),
                            "previous_round_started_at": job.get("round_started_at"),
                            "previous_task_id": job["ov_task_id"]})
            recovered = self.update_job(job_id, status="preparing", error="", finished_at="",
                native_contract_recoveries=upgrades, recovery_history=history, native_repair_instruction="",
                **({"native_explicit_retry": meta} if authorization is not None else {}),
                compile_request={}, ov_task_id="", ov_status="", ov_stage="", ov_pipeline={}, materialized=False,
                submission_idempotency_key=journal["submission_key"], submission_started_at="",
                ov_task_created_at="", ov_task_started_at="", ov_task_updated_at="", ov_processing_seconds=None,
                ov_sync_error="", ov_sync_failures=0, ov_next_retry_at="", materialize_attempts=0,
                round_started_at=(job.get("round_started_at") if authorization is not None
                                  else journal["budget_started_at"]))
            self.append_log(job_id, "已核验远端终态、冻结来源及历史产物；"
                            + ("管理员授权本次人工重试，原预算保留，追加独立尝试预算" if authorization is not None
                               else "保留原编译 Skill、旧修订次数和已有恢复预算，以新版请求契约恢复"))
            return recovered

    def resume_identity_failure(self, job_id: str) -> dict[str, Any]:
        """Recover the identified host error at its durable boundary, preserving frozen requests."""
        with self._lock:
            job = self.get_job(job_id)
            if job.get("status") == "preparing" and (job.get("recovery_history") or []):
                return job
            from .artifact_recovery import ERROR_PREFIX, prepare_recovery
            from .extract_recovery import ERROR_PREFIX as EXTRACT_ERROR_PREFIX
            from .extract_recovery import prepare_recovery as prepare_extract_recovery
            from .intent_recovery import ERROR as INTENT_ERROR
            from .intent_recovery import prepare_recovery as prepare_intent_recovery
            from .routing_recovery import ERROR as ROUTING_ERROR
            from .routing_recovery import prepare_recovery as prepare_routing_recovery

            capacity = str(job.get("error", "")).startswith(ERROR_PREFIX)
            extraction = str(job.get("error", "")).startswith(EXTRACT_ERROR_PREFIX)
            routing = job.get("error") == ROUTING_ERROR
            intent = job.get("error") == INTENT_ERROR
            if job.get("status") != "failed" or (job.get("error") != RECOVERABLE_IDENTITY_ERROR
                                                 and not capacity and not extraction and not routing and not intent):
                raise ValueError("此任务不属于已修复的可恢复失败")
            if job.get("ov_cancel_reason"):
                raise ValueError("已请求停止的任务不能自动恢复")
            path = self._job_dir(job_id) / "round_inputs" / f"round-{job['current_round']}" / "staged/state.json"
            if path.is_symlink():
                raise ValueError("恢复状态必须来自当前任务的普通冻结文件")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not capacity and not extraction and not routing and not intent and (
                    state.get("round") != job["current_round"]
                    or state.get("phase") != "identity_group"
                    or state.get("current") is not None or state.get("complete")):
                raise ValueError("任务不在可恢复的候选身份复核边界")
            active = sum(row.get("status") in ACTIVE_JOB_STATES for row in self._iter_jobs_unlocked())
            if active >= MAX_PARALLEL_JOBS:
                raise RuntimeError("运行并发已满，请稍后恢复")
            journal = prepare_recovery(path.parent, state, job, apply=True) if capacity else None
            if extraction:
                journal = prepare_extract_recovery(path.parent, state, job, apply=True)
            if routing:
                journal = prepare_routing_recovery(path.parent, state, job, apply=True)
            if intent:
                journal = prepare_intent_recovery(path.parent, state, job, apply=True)
            history = list(job.get("recovery_history") or [])
            history.append({"round": job["current_round"], "error": job["error"], "failed_at": job["finished_at"],
                            "resumed_at": utc_now(),
                            "reason": journal["reason"] if journal else "候选名称冲突已修复，从身份复核断点继续",
                            **({"recovery_protocol": journal["protocol"]} if journal else {})})
            recovered = self.update_job(job_id, status="preparing", error="", finished_at="", ov_task_id="",
                                        ov_status="", recovery_history=history)
            self.append_log(job_id, "从已修复失败断点恢复：保留原始来源、已受理请求、产物及未解决问题，从当前轮继续")
            return recovered

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
    "MAX_CANDIDATES",
    "PLAN_SCHEMA_VERSION",
    "LEGACY_SKILL_BODY",
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
