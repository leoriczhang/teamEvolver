# ruff: noqa: F401
# Imports are part of the explicit compatibility surface consumed by split mixins.
"""Durable per-round OV submissions; human waiting belongs to the TE control plane."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import httpx
import yaml
from starlette.exceptions import HTTPException

from team_miner.shared.document_converter import convert_knowledge_upload

from .content_io import _content_text
from .native_round import NativeRoundMixin
from .output_language import output_language_instruction, resolve_output_language_policy
from .ov_compile_mining import (
    LEGACY_SKILL_BODY,
    PLAN_SCHEMA_VERSION,
    RUNS_ROOT,
    SKILL_BODY,
    SKILL_NAME,
    utc_now,
    validate_resource_uri,
)
from .ov_staged_orchestration import OvStagedOrchestrationMixin
from .platform_client import CompileClientCompat as CompileClient

MAX_INPUT_FILES = 10_000
MAX_SOURCE_FILE_BYTES = 20 * 1024 * 1024
# Original Evidence, cumulative supplements and revision archives have separate bounds.
# A full-size first round must not consume the room needed to revise it in later rounds.
INPUT_LIMITS = {
    "source": (MAX_INPUT_FILES, None, "原始来源"),
    "human": (256, 32 * 1024 * 1024, "累积补充 Evidence"),
    "context": (128, 16 * 1024 * 1024, "上一轮修订上下文"),
}


def _temporary_resource_lock(exc: Exception) -> bool:
    """A resource lock conflict is transient; payload/idempotency conflicts remain definitive."""
    return (
        isinstance(exc, HTTPException)
        and exc.status_code in {409, 423}
        and re.search(r"\block acquire timed out after \d+(?:\.\d+)?\s*ms\b", str(exc.detail), re.I) is not None
    )


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _staged_attachment_reference(job: dict, attachment: dict, job_dir: Path) -> str:
    """Resolve only the current checkpoint's exact staged attachment, before OV upload."""
    round_number = int(job["current_round"])
    pending = job.get("pending_checkpoint") or {}
    if job.get("status") != "waiting_human" or not pending.get("id") or pending.get("round") != round_number:
        raise ValueError("补充附件不属于当前待复核轮次")
    name = attachment.get("name")
    digest = attachment.get("sha256")
    if (
        not isinstance(name, str)
        or PurePosixPath(name).name != name
        or "\\" in name
        or name.startswith(".")
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or not name.endswith(f"-{digest[:12]}.md")
    ):
        raise ValueError("补充附件名称或摘要无效")
    expected_uri = (
        f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{round_number + 1}/source/supplements/round-{round_number}/{name}"
    )
    if attachment.get("uri") != validate_resource_uri(expected_uri):
        raise ValueError("补充附件 URI 不属于当前任务及轮次")
    expected_path = job_dir / "attachments" / f"round-{round_number}" / name
    try:
        path = Path(attachment.get("local_path") or "")
        if path != expected_path or path.is_symlink() or path.resolve() != expected_path or not path.is_file():
            raise ValueError("补充附件路径越界或不是本任务的普通文件")
        with path.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ValueError("无法读取当前任务的补充附件") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("引用 Evidence 文件不能超过 2 MB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("补充附件不是有效 UTF-8 文本") from exc
    if _digest(content) != digest or attachment.get("content_bytes") != len(raw):
        raise ValueError("补充 Evidence 附件内容或大小已被修改")
    return content


def _validate_compiler_contract(job: dict, body: str) -> None:
    if int(job.get("candidate_contract_version") or 1) >= 2 and PLAN_SCHEMA_VERSION not in body:
        raise ValueError(
            "该任务使用多候选编译契约，但当前编译 Skill 未声明 skill-miner.plan.v1；"
            "请恢复支持能力拆分计划的编译 Skill 后重新发起任务。"
        )


def _compiler_package(source_body: str) -> tuple[str, str, str]:
    """Bind a frozen compiler to its own namespace and native stored Markdown."""
    match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n?(.*)", source_body, flags=re.DOTALL)
    if match is None:
        raise ValueError("内部编译 Skill 缺少合法 frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("description"), str):
        raise ValueError("内部编译 Skill frontmatter 缺少 description")
    # Internal compiler packages have no attachments, tool declarations or optional
    # metadata. Match OV's SkillLoader serialization rather than hashing a body
    # which ingestion would later normalize (including its final newline).
    if set(metadata) != {"name", "description"} or not isinstance(metadata.get("name"), str):
        raise ValueError("内部编译 Skill frontmatter 必须包含 name 和 description")
    source_sha256 = _digest(source_body)
    package_name = f"{SKILL_NAME}-{source_sha256[:32]}"
    frontmatter = {"name": package_name, "description": metadata["description"]}
    published_body = (
        "---\n" + yaml.dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n\n" + match.group(2).strip()
    )
    return package_name, published_body, source_sha256


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _cache_inputs(root: Path, files: list[dict]) -> list[dict]:
    _validate_input_limits(files)
    root.mkdir(parents=True, exist_ok=True)
    entries = [_cache_input(root, item, index) for index, item in enumerate(files)]
    _write_json(root / "manifest.json", entries)
    return entries


def _cache_input(root: Path, item: dict, index: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    content = item["content"]
    path = root / f"{index:03d}-{_digest(content)}.md"
    path.write_text(content, encoding="utf-8")
    return {
        **{key: value for key, value in item.items() if key != "content"},
        "local_path": str(path),
        "sha256": _digest(content),
        "content_bytes": len(content.encode("utf-8")),
        "content_chars": len(content),
    }


def _append_input(files: list[dict], item: dict) -> None:
    # Check before accumulating another file: the bound must also hold during freezing.
    _validate_input_limits([*files, item])
    if any(row["kind"] == item["kind"] and row["relative_path"] == item["relative_path"] for row in files):
        raise ValueError("本轮冻结输入存在重复目标路径")
    files.append(item)


def _validate_input_limits(files: list[dict]) -> None:
    totals = {kind: [0, 0] for kind in INPUT_LIMITS}
    for item in files:
        kind = item.get("kind")
        if kind not in totals:
            raise ValueError("冻结输入的来源类型无效")
        totals[kind][0] += 1
        size = len(item["content"].encode("utf-8")) if "content" in item else int(item["content_bytes"])
        if kind == "source" and size > MAX_SOURCE_FILE_BYTES:
            raise ValueError("原始来源的单个文件不能超过 20 MB")
        totals[kind][1] += size
    for kind, (count, size) in totals.items():
        max_count, max_size, label = INPUT_LIMITS[kind]
        if count > max_count:
            raise ValueError(f"{label}超过 {max_count} 个文件")
        if max_size is not None and size > max_size:
            raise ValueError(f"{label}超过 {max_size // (1024 * 1024)} MB")


def _cache_locator(root: Path, saved: str, *, expected_name: str = "") -> Path:
    """Resolve a frozen host locator without rewriting its hash-bound manifest."""
    if not isinstance(saved, str) or not Path(saved).is_absolute() or root.is_symlink():
        raise ValueError("冻结缓存定位无效")
    original = Path(saved)
    if expected_name and original.name != expected_name:
        raise ValueError("冻结缓存文件名已改变")
    current = root / original.name
    if original.parent != root:
        job_root = next((parent for parent in root.parents if parent.parent.name == "mining_jobs"), None)
        if job_root is None:
            raise ValueError("冻结缓存目录不属于挖掘任务")
        suffix = root.relative_to(job_root).as_posix() + "/" + original.name
        if not str(original).endswith(f"/mining_jobs/{job_root.name}/{suffix}"):
            raise ValueError("冻结缓存定位不属于当前任务及目录")
    if current.is_symlink() or root.resolve() not in current.resolve().parents:
        raise ValueError("冻结缓存定位包含符号链接或越界")
    return current


def _cached_inputs(root: Path) -> list[dict] | None:
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return None
    if manifest.is_symlink():
        raise ValueError("本轮冻结输入清单不能是符号链接")
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for item in entries:
        path = _cache_locator(root, item["local_path"])
        if _digest(path.read_text(encoding="utf-8")) != item["sha256"]:
            raise ValueError("本轮冻结输入的完整性校验失败")
        item["local_path"] = str(path)
    return entries


def _skill_excerpt_and_digest(path: Path) -> tuple[str, str]:
    """Bound repeated revision text while hashing every byte of the archived Skill."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    with path.open("r", encoding="utf-8") as stream:
        excerpt = stream.read(24_000)
    return excerpt, digest.hexdigest()


def _previous_candidate_catalog(root: Path) -> list[dict] | None:
    """Read local candidate locators without accumulating all prior Skill bodies."""
    if root.is_symlink():
        raise ValueError("上一轮候选目录不能是符号链接")
    path = root / "manifest.json"
    if not path.is_file():
        return None
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("上一轮候选目录清单无效")
    seen = set()
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if (
            not isinstance(name, str)
            or len(name) > 80
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or name in seen
        ):
            raise ValueError("上一轮候选目录名称无效或重复")
        seen.add(name)
        candidate_path = _cache_locator(root, row.get("local_path"), expected_name=f"{name}.json")
        if (
            not candidate_path.is_file()
            or _digest(candidate_path.read_text(encoding="utf-8")) != row.get("sha256")
        ):
            raise ValueError("上一轮候选目录完整性校验失败")
        row["local_path"] = str(candidate_path)
    return rows


def _freeze_previous_candidates(job: dict, job_dir: Path, root: Path, preceding: list[dict]) -> list[dict]:
    """Retain identities locally; only exact frozen source paths can bind a later assignment."""
    cached = _previous_candidate_catalog(root)
    if cached is not None:
        return cached
    previous_number = int(job["current_round"]) - 1
    archive = job_dir / "rounds" / f"round-{previous_number}"
    reviewed = archive / "reviewed_output"
    if not reviewed.is_dir() or reviewed.is_symlink():
        raise ValueError("缺少上一轮已复核产物")
    # The archived input metadata supplies the source namespace, rather than a
    # candidate-provided URI, mutable live source tree or filename approximation.
    archived_input = json.loads((archive / "input.json").read_text(encoding="utf-8"))
    prefix = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{previous_number}/source"
    if archived_input.get("source_snapshot") != prefix:
        raise ValueError("上一轮来源快照不属于当前任务及轮次")
    original_prefix = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-1/source"
    if archived_input.get("original_source_snapshot") != original_prefix:
        raise ValueError("第一轮来源快照不属于当前任务")
    reference_paths: dict[str, set[str]] = {}
    for row in preceding:
        if row["kind"] not in {"source", "human"}:
            continue
        relative = row["relative_path"]
        if not relative.startswith(("original/", "supplements/")) or any(
            part in {"", ".", ".."} or "\\" in part for part in relative.split("/")
        ):
            raise ValueError("上一轮冻结来源相对路径无效")
        references = [f"{prefix}/{relative}"]
        if row["kind"] == "source" and relative.startswith("original/"):
            references.append(f"{original_prefix}/{relative}")
        if row.get("origin_uri"):
            references.append(row["origin_uri"])
        for reference in references:
            reference_paths.setdefault(reference, set()).add(relative)
    plan_path = reviewed / "semantic_reports" / "capability-plan.json"
    if not plan_path.is_file():
        plan_path = reviewed / "analysis" / "capability-plan.json"
    if plan_path.is_file() and (plan_path.is_symlink() or reviewed.resolve() not in plan_path.resolve().parents):
        raise ValueError("上一轮能力拆分计划归档路径越界")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
        if not isinstance(plan, dict) or not isinstance(plan.get("skills", []), list):
            plan = {}
    except (ValueError, TypeError):
        # An invalid prior proposal can be repaired in the next round. Its bytes
        # stay archived; absent lineage must never be guessed from its filename.
        plan = {}
    planned = {
        row["name"]: row
        for row in plan.get("skills") or []
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    actual = {path.parent.name for path in (reviewed / "compiled_skill").glob("*/SKILL.md") if path.is_file()}
    entries = []
    for name in sorted(set(planned) | actual):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 80:
            raise ValueError("上一轮候选名称无效")
        candidate = planned.get(name) or {}
        body_path = reviewed / "compiled_skill" / name / "SKILL.md"
        dataset_path = body_path.with_name("benchmark.json")
        if (
            body_path.is_symlink()
            or dataset_path.is_symlink()
            or any(
                path.is_file() and reviewed.resolve() not in path.resolve().parents
                for path in (body_path, dataset_path)
            )
        ):
            raise ValueError("上一轮候选归档文件不能是符号链接")
        excerpt, digest = _skill_excerpt_and_digest(body_path) if body_path.is_file() else ("", "")
        try:
            document = json.loads(dataset_path.read_text(encoding="utf-8")) if dataset_path.is_file() else {}
            case_ids = [str(case.get("case_id") or "") for case in document.get("cases") or []]
        except (ValueError, AttributeError, TypeError):
            case_ids = []
        refs = candidate.get("source_refs")
        refs = list(refs) if isinstance(refs, list) else []
        paths = []
        for uri in refs:
            mapped = reference_paths.get(uri, set()) if isinstance(uri, str) else set()
            if len(mapped) == 1:
                relative = next(iter(mapped))
                if relative not in paths:
                    paths.append(relative)
        value = {
            "name": name,
            "case_ids": case_ids,
            "task": str(candidate.get("task") or ""),
            "boundary": str(candidate.get("boundary") or ""),
            "source_refs": refs,
            "scope_relative_paths": paths,
            "skill_body_excerpt": excerpt,
            "skill_body_sha256": digest,
            "skill_body_path": str(body_path) if body_path.is_file() else "",
        }
        candidate_path = root / f"{name}.json"
        _write_json(candidate_path, value)
        entries.append(
            {
                "name": name,
                "local_path": str(candidate_path),
                "sha256": _digest(candidate_path.read_text(encoding="utf-8")),
            }
        )
    _write_json(root / "manifest.json", entries)
    return entries


def _round_execution_budget(job: dict, entries: list[dict], compiler_body: str) -> dict:
    """Scale a finite round deadline with the number of bounded Evidence assignments."""
    base = int(job.get("runtime_timeout_seconds") or 3600)
    if "te-staged-compile.v1" not in compiler_body:
        return {"round_timeout_seconds": base, "round_work_units": 1}
    units = sum(
        max(
            1,
            math.ceil(
                int(item.get("content_chars") or item.get("content_bytes") or Path(item["local_path"]).stat().st_size)
                / 24_000
            ),
        )
        for item in entries
        if item["kind"] != "context"
    )
    return {"round_work_units": units, "round_timeout_seconds": base * max(1, math.ceil(units / 8))}
