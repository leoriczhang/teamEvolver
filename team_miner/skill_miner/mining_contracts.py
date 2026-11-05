# ruff: noqa: F401
# Imports are part of the explicit compatibility surface consumed by split mixins.
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
