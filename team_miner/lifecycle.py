"""Bridge SkillMiner artifacts into teamEvolver's review/publish lifecycle.

The mining pipeline writes a self-contained skill bundle below
``skillminer/compiled_skill``.  teamEvolver deliberately does not install those
files directly: this module turns them into an idempotent validation candidate
so A/B evaluation and the existing human publish gate remain mandatory.

``benchmark.json`` uses the canonical Test Dataset contract for this
hand-off. Only the shared Test Dataset contract is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from team_replay.contracts import DOCUMENT_REPLAY_RUNTIME, document_replay_execution
from team_replay.datasets.schema import normalize_case
from team_skills.candidates.store import ValidationStore
from team_skills.library.bundle import bundle_tree_sha256, encode_bundle_payload, read_skill_bundle
from team_skills.library.frontmatter import parse_skill_md
from team_skills.library.render import build_skill_md

from . import benchmark_format as progressive_benchmark

SKILLMINER_ROOT = Path(__file__).resolve().parent
INTERNAL_BENCHMARK_FORMAT = progressive_benchmark.DATASET_FORMAT
MINED_REPLAY_BINDING_VERSION = 1


class MiningLifecycleError(ValueError):
    """Raised when a mined artifact cannot enter the review lifecycle."""


def _compiled_root(skillminer_root: Path | str = SKILLMINER_ROOT) -> Path:
    return Path(skillminer_root).resolve() / "compiled_skill"


def resolve_mined_job_workspace(
    job_id: str,
    *,
    skillminer_root: Path | str = SKILLMINER_ROOT,
) -> Path:
    """Resolve the immutable workspace of one completed mining job safely."""
    raw_id = str(job_id or "").strip()
    if not raw_id:
        raise MiningLifecycleError("job_id is required")
    jobs_root = Path(skillminer_root).resolve() / "mining_jobs"
    job_dir = (jobs_root / raw_id).resolve()
    if job_dir.parent != jobs_root:
        raise MiningLifecycleError("挖掘任务 ID 无效")
    metadata_path = job_dir / "job.json"
    if not metadata_path.is_file():
        raise MiningLifecycleError(f"未找到挖掘任务：{raw_id}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MiningLifecycleError(f"挖掘任务元数据损坏：{raw_id}") from exc
    if not isinstance(metadata, dict) or str(metadata.get("job_id") or raw_id) != raw_id:
        raise MiningLifecycleError(f"挖掘任务元数据不匹配：{raw_id}")
    if str(metadata.get("status") or "") != "succeeded":
        raise MiningLifecycleError("只有已完成的挖掘任务可以提交到进化候选区")
    workspace = job_dir / "workspace"
    if not (workspace / "compiled_skill").is_dir():
        raise MiningLifecycleError("挖掘任务没有可提交的已编译 Skill")
    return workspace


def resolve_mined_job_skill_root(
    job_id: str,
    skill_name: str,
    *,
    artifact_path: str = "",
    skillminer_root: Path | str = SKILLMINER_ROOT,
) -> Path:
    """Resolve a completed OV job for candidate submission."""
    del artifact_path
    workspace = resolve_mined_job_workspace(job_id, skillminer_root=skillminer_root)
    metadata = json.loads((workspace.parent / "job.json").read_text(encoding="utf-8"))
    if int(metadata.get("candidate_contract_version") or 1) >= 2:
        from .ov_compile_mining import OvSkillMiningRepository

        # Revalidate the selected current bundle after human edits. Sibling candidates
        # have independent validation outcomes and never block this submission.
        repository = OvSkillMiningRepository(workspace.parent / "submission_state", skillminer_root)
        try:
            plan = repository._read_capability_plan(metadata)
            if skill_name not in {row["name"] for row in plan["skills"]}:
                raise ValueError("该 Skill Candidate 不在能力拆分计划中")
            repository._validate_candidate(metadata, resolve_mined_skill_dir(skill_name, skillminer_root=workspace))
        except ValueError as exc:
            raise MiningLifecycleError(str(exc)) from exc
    return workspace


def resolve_mined_skill_dir(
    skill_name: str,
    *,
    skillminer_root: Path | str = SKILLMINER_ROOT,
) -> Path:
    """Resolve a mined skill by directory name without allowing path escape."""
    raw_name = str(skill_name or "").strip()
    if not raw_name:
        raise MiningLifecycleError("skill_name is required")
    root = _compiled_root(skillminer_root)
    candidate = (root / raw_name).resolve()
    if candidate.parent != root or not (candidate / "SKILL.md").is_file():
        raise MiningLifecycleError(f"未找到已编译 Skill：{raw_name}")
    return candidate


def load_internal_benchmark(skill_dir: Path | str) -> list[dict[str, Any]]:
    """Load canonical cases directly for Candidate Review and True Replay."""
    path = Path(skill_dir) / "benchmark.json"
    if not path.is_file():
        raise MiningLifecycleError("缺少 benchmark.json，请先生成 Test Dataset")
    payload, errors = progressive_benchmark.read_document(path)
    if payload is None or errors:
        raise MiningLifecycleError("benchmark.json 不可用：" + "；".join(errors))
    return payload["cases"]


def benchmark_replay_cases(skill_name: str, cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bind document cases to tenant execution without inventing source Sessions."""
    result = []
    for raw in cases:
        case = normalize_case({**raw, "skill_ids": [skill_name]})
        # Evidence URI references are not recorded Sessions. Neither a compiler
        # nor an edited Dataset can supply an execution identity or private Context.
        for key in (
            "session_id", "session_snapshot", "source_runtime", "source_runtime_context",
            "context_snapshot", "context_snapshot_id",
        ):
            case["provenance"].pop(key, None)
        case["replay"] = {
            "progressive_disclosure": case["replay"]["progressive_disclosure"],
            "execution": document_replay_execution(),
        }
        result.append(case)
    return result


def _candidate_skill_payload(parsed: dict[str, Any], bundle: dict[str, bytes]) -> dict[str, Any]:
    bundle_files: dict[str, str] = {}
    for rel_path, data in sorted(bundle.items()):
        if rel_path == "SKILL.md":
            continue
        try:
            bundle_files[rel_path] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MiningLifecycleError(f"候选包包含非 UTF-8 文件：{rel_path}") from exc
    candidate = {
        "name": str(parsed.get("name") or ""),
        "description": str(parsed.get("description") or ""),
        "category": str(parsed.get("category") or "general"),
        "content": str(parsed.get("content") or ""),
    }
    extra = parsed.get("_extra_frontmatter")
    if isinstance(extra, dict) and extra:
        candidate["extra_frontmatter"] = extra
    metadata = parsed.get("metadata")
    if isinstance(metadata, dict) and metadata:
        candidate["metadata"] = metadata
    # OV accepts folded YAML descriptions; Agent execution requires the canonical
    # single-line representation. Normalize only the candidate entrypoint, keeping
    # the raw mined bundle and its source artifact hash intact for audit.
    try:
        entrypoint = build_skill_md(candidate).encode("utf-8")
    except ValueError as exc:
        raise MiningLifecycleError(f"SKILL.md 无法规范化为执行格式：{exc}") from exc
    candidate["bundle"] = encode_bundle_payload({**bundle, "SKILL.md": entrypoint})
    return candidate


def _current_skill_payload(current_skill: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(current_skill, dict) or not current_skill.get("name"):
        return None
    payload = {
        "name": str(current_skill.get("name") or ""),
        "description": str(current_skill.get("description") or ""),
        "category": str(current_skill.get("category") or "general"),
        "content": str(current_skill.get("content") or ""),
    }
    extra = current_skill.get("_extra_frontmatter") or current_skill.get("extra_frontmatter")
    if isinstance(extra, dict) and extra:
        payload["extra_frontmatter"] = extra
    if isinstance(current_skill.get("bundle"), dict):
        payload["bundle"] = current_skill["bundle"]
    return payload


def mined_artifact_descriptor(
    skill_name: str,
    *,
    skillminer_root: Path | str = SKILLMINER_ROOT,
) -> dict[str, Any]:
    """Read one compiled skill and return its lifecycle-ready metadata."""
    skill_dir = resolve_mined_skill_dir(skill_name, skillminer_root=skillminer_root)
    parsed = parse_skill_md(str(skill_dir / "SKILL.md"))
    if not parsed:
        raise MiningLifecycleError("SKILL.md 缺少有效的 name / description frontmatter")
    if str(parsed.get("name") or "") != skill_dir.name:
        raise MiningLifecycleError(
            f"SKILL.md name（{parsed.get('name')}）与目录名（{skill_dir.name}）不一致"
        )
    bundle = read_skill_bundle(skill_dir)
    questions = load_internal_benchmark(skill_dir)
    return {
        "name": skill_dir.name,
        "skill_dir": skill_dir,
        "parsed": parsed,
        "bundle": bundle,
        "artifact_sha256": bundle_tree_sha256(bundle),
        "questions": questions,
        "question_count": len(questions),
        "has_evaluation": (skill_dir / "EVALUATION.md").is_file(),
    }


def _matching_job(
    store: ValidationStore,
    *,
    skill_name: str,
    artifact_sha256: str,
) -> Optional[dict[str, Any]]:
    for job in reversed(store.list_jobs()):
        source = job.get("source") if isinstance(job.get("source"), dict) else {}
        if (
            source.get("kind") == "skillminer"
            and str(source.get("skill_name") or "") == skill_name
            and str(source.get("artifact_sha256") or "") == artifact_sha256
            and source.get("replay_binding_version") == MINED_REPLAY_BINDING_VERSION
        ):
            return job
    return None


def submit_mined_skill(
    store: ValidationStore,
    skill_name: str,
    *,
    current_skill: Optional[dict[str, Any]] = None,
    submitted_by: str = "",
    skillminer_root: Path | str = SKILLMINER_ROOT,
    mining_job_id: str = "",
) -> dict[str, Any]:
    """Create (or return) an idempotent human-review candidate job."""
    artifact = mined_artifact_descriptor(skill_name, skillminer_root=skillminer_root)
    if not artifact["has_evaluation"]:
        raise MiningLifecycleError("缺少 EVALUATION.md，不能提交候选评审")

    existing = _matching_job(
        store,
        skill_name=artifact["name"],
        artifact_sha256=artifact["artifact_sha256"],
    )
    if existing:
        decision = store.load_decision(str(existing.get("job_id") or ""))
        return {"created": False, "job": existing, "decision": decision}

    current = _current_skill_payload(current_skill)
    job_id = store.make_job_id(artifact["name"])
    job = {
        "job_id": job_id,
        "skill_name": artifact["name"],
        "candidate_skill_name": artifact["name"],
        "proposed_action": "update" if current else "create",
        "rationale": (
            f"由 SkillMiner 编译并通过 {artifact['question_count']} 条 Test Dataset 案例完整性检查；"
            "等待 True Replay 与 Candidate Review。"
        ),
        "candidate_skill": _candidate_skill_payload(artifact["parsed"], artifact["bundle"]),
        "current_skill": current,
        "replay_cases": benchmark_replay_cases(artifact["name"], artifact["questions"]),
        "runtime_validation_policy": {
            "declaration": "document_case",
            "execution": document_replay_execution(),
            "required_runtimes": [DOCUMENT_REPLAY_RUNTIME],
            "distribution_runtimes": [],
        },
        "min_score": 0.75,
        "source": {
            "kind": "skillminer",
            "skill_name": artifact["name"],
            "artifact_sha256": artifact["artifact_sha256"],
            "dataset_format": INTERNAL_BENCHMARK_FORMAT,
            "question_count": artifact["question_count"],
            "submitted_by": str(submitted_by or ""),
            "replay_binding_version": MINED_REPLAY_BINDING_VERSION,
        },
    }
    if mining_job_id:
        job["source"]["mining_job_id"] = str(mining_job_id)
    store.save_job(job)
    return {"created": True, "job": job, "decision": None}


def list_mined_skill_statuses(
    store: ValidationStore,
    *,
    registered_skill_names: Iterable[str] = (),
    skillminer_root: Path | str = SKILLMINER_ROOT,
) -> list[dict[str, Any]]:
    """Return compiled artifacts annotated with candidate/publish state."""
    root = _compiled_root(skillminer_root)
    if not root.is_dir():
        return []
    registered = {str(name) for name in registered_skill_names}
    jobs = store.list_jobs()
    rows: list[dict[str, Any]] = []
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        try:
            artifact = mined_artifact_descriptor(skill_dir.name, skillminer_root=skillminer_root)
            match = next(
                (
                    job
                    for job in reversed(jobs)
                    if isinstance(job.get("source"), dict)
                    and job["source"].get("kind") == "skillminer"
                    and job["source"].get("artifact_sha256") == artifact["artifact_sha256"]
                ),
                None,
            )
            decision = store.load_decision(str(match.get("job_id") or "")) if match else None
            if decision and decision.get("status") == "published":
                status = "published"
            elif decision:
                status = "rejected"
            elif match:
                status = "candidate"
            else:
                status = "ready"
            rows.append(
                {
                    "name": skill_dir.name,
                    "status": status,
                    "job_id": str(match.get("job_id") or "") if match else "",
                    "artifact_sha256": artifact["artifact_sha256"],
                    "question_count": artifact["question_count"],
                    "dataset_format": INTERNAL_BENCHMARK_FORMAT,
                    "registered": skill_dir.name in registered,
                    "error": "",
                }
            )
        except MiningLifecycleError as exc:
            rows.append(
                {
                    "name": skill_dir.name,
                    "status": "incomplete",
                    "job_id": "",
                    "artifact_sha256": "",
                    "question_count": 0,
                    "dataset_format": INTERNAL_BENCHMARK_FORMAT,
                    "registered": skill_dir.name in registered,
                    "error": str(exc),
                }
            )
    return rows
