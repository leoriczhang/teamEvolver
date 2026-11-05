"""Submit complete PG mining snapshots to the existing Candidate Review lifecycle."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from team_replay.datasets.schema import DATASET_SCHEMA, normalize_case
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.config import load_skills_config
from team_skills.evolution.history import skill_payload
from team_skills.evolution.repository import lock_key
from team_skills.library import frontmatter
from team_skills.library.bundle import bundle_tree_sha256, diff_skill_bundles
from team_skills.library.repository import SkillLibraryRepository, validate_bundle, validate_skill_name
from teamEvolver.config_runtime import ConfigRepository

from . import benchmark_format
from .candidate_materials import hydrate, materialize
from .dataset_view import dataset_markdown
from .mining_completion import completion_only
from .repository import MiningRevisionConflict, decode, identity
from .storage import file_path, validate_files

BINDING_VERSION = 3
# Match the Native Compile single-file contract; checking metadata before
# fetching BYTEA must not reduce the existing mining output capacity.
MAX_CAPABILITY_PLAN_BYTES = 20 * 1024 * 1024
DOCUMENT_POLICY = {
    "type": "document", "declaration": "document_case", "binding": "tenant_replay_adapter",
    "required_runtimes": ["document"], "distribution_runtimes": [],
}
_PRIVATE_SOURCE_FIELDS = {
    "session_id", "session_snapshot", "source_runtime", "source_runtime_context",
    "context_snapshot", "context_snapshot_id", "runtime", "runtime_context",
}


@dataclass(frozen=True)
class MiningCandidateSnapshot:
    job: dict
    revision: int
    files_revision: int
    workspace: str
    files: dict[str, bytes]


class MiningCandidateRepository:
    """Read and guard only the mining domain's metadata and artifact tables."""

    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def load(self, conn, tenant_id, job_id, skill_name, *, limits):
        row = await conn.fetchrow(
            f"SELECT revision,files_revision,document FROM {self.schema}.skill_mining_jobs "
            "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", tenant_id, job_id,
        )
        if row is None:
            raise KeyError(job_id)
        job = decode(row["document"])
        if job.get("job_id") != job_id:
            raise ValueError("mining metadata identity mismatch")
        workspace = str(job.get("workspace") or "")
        prefix = f"mining_jobs/{job_id}/"
        if not workspace.startswith(prefix):
            raise ValueError("invalid mining workspace")
        workspace = file_path(workspace[len(prefix):])
        bundle_prefix = f"{workspace}/compiled_skill/{skill_name}/"
        plan_path = f"{workspace}/semantic_reports/capability-plan.json"
        # Metadata is small even when a legal mining job contains very large
        # archival outputs. Reject this candidate before transferring any BYTEA.
        stats = await conn.fetchrow(
            f"SELECT count(*) FILTER (WHERE path<>$4) AS file_count,"
            "COALESCE(sum(size) FILTER (WHERE path<>$4),0) AS total_bytes,"
            "COALESCE(max(size) FILTER (WHERE path<>$4),0) AS max_file_bytes,"
            "COALESCE(max(size) FILTER (WHERE path=$4),0) AS plan_bytes "
            f"FROM {self.schema}.skill_mining_files WHERE tenant_id=$1 AND job_id=$2 "
            "AND (left(path,length($3))=$3 OR path=$4)", tenant_id, job_id, bundle_prefix, plan_path,
        )
        if (stats["file_count"] > limits.max_files or stats["total_bytes"] > limits.max_bytes
                or stats["max_file_bytes"] > limits.max_file_bytes):
            raise ValueError("mined candidate exceeds the configured Skill bundle limits")
        if stats["plan_bytes"] > MAX_CAPABILITY_PLAN_BYTES:
            raise ValueError("mining capability plan exceeds its size limit")
        records = await conn.fetch(
            f"SELECT path,sha256,size,content FROM {self.schema}.skill_mining_files "
            "WHERE tenant_id=$1 AND job_id=$2 AND (left(path,length($3))=$3 OR path=$4) ORDER BY path",
            tenant_id, job_id, bundle_prefix, plan_path,
        )
        files = {}
        for record in records:
            content = bytes(record["content"])
            if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("mining artifact content hash mismatch")
            files[file_path(record["path"])] = content
        return MiningCandidateSnapshot(job, int(row["revision"]), int(row["files_revision"]),
                                       workspace, validate_files(files))

    async def require_snapshot(self, conn, tenant_id, snapshot):
        row = await conn.fetchrow(
            f"SELECT revision,files_revision FROM {self.schema}.skill_mining_jobs "
            "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR UPDATE",
            tenant_id, snapshot.job["job_id"],
        )
        if (row is None or int(row["revision"]) != snapshot.revision
                or int(row["files_revision"]) != snapshot.files_revision):
            raise MiningRevisionConflict("mining artifacts changed; reload before submitting the candidate")


def document_cases(skill_name, cases):
    """Bind source documents to the tenant's fixed adapter without invented Sessions."""
    result = []
    for raw in cases:
        case = normalize_case({**copy.deepcopy(raw), "skill_ids": [skill_name]})
        for key in _PRIVATE_SOURCE_FIELDS:
            case["provenance"].pop(key, None)
        # Compiler-provided adapter, execution, private Context and runtime choices
        # cannot survive this boundary. Real Evidence URI citations remain intact.
        case["replay"] = {"progressive_disclosure": case["replay"]["progressive_disclosure"]}
        result.append(case)
    return result


def _canonical_bundle(skill_name, bundle):
    raw = bundle["SKILL.md"].decode("utf-8")
    split = frontmatter.split_skill_md(raw, strict=True)
    if split is None:
        raise ValueError("SKILL.md requires frontmatter")
    fields = frontmatter._load_frontmatter_from_raw(raw)
    if not fields or fields.get("name") != skill_name:
        raise ValueError("SKILL.md name must match its directory")
    return {**bundle, "SKILL.md": frontmatter.render_skill_md(
        name=skill_name, description=fields.get("description"), content=split[1],
        category=frontmatter.resolve_category(fields) or "general", metadata=fields.get("metadata"),
        extra_frontmatter={key: value for key, value in fields.items()
                           if key not in {"name", "description", "category", "metadata"}},
    ).encode("utf-8")}


def _prepare(repository, snapshot, skill_name, artifact_path, limits):
    job = snapshot.job
    if job.get("status") != "succeeded":
        raise ValueError("只有已完成的挖掘任务可以提交到进化候选区")
    if completion_only(job) or job.get("content_validated") is False:
        raise ValueError("当前产物未执行内容验收，不能提交候选评审；需要完整的评测与 Test Dataset")
    expected_entry = f"mining_jobs/{job['job_id']}/{snapshot.workspace}/compiled_skill/{skill_name}/SKILL.md"
    if artifact_path and artifact_path != expected_entry:
        raise ValueError("artifact_path does not identify the current candidate SKILL.md")
    prefix = f"{snapshot.workspace}/compiled_skill/{skill_name}/"
    bundle = {name[len(prefix):]: content for name, content in snapshot.files.items() if name.startswith(prefix)}
    if "SKILL.md" not in bundle:
        raise ValueError("未找到当前任务的 Skill Candidate")
    # Bounds apply before materializing the disposable validation view. The
    # canonical bundle is checked again by CandidateRepository in its transaction.
    if (len(bundle) > limits.max_files or sum(map(len, bundle.values())) > limits.max_bytes
            or any(len(content) > limits.max_file_bytes for content in bundle.values())):
        raise ValueError("mined candidate exceeds the configured Skill bundle limits")
    project = Path(repository.project_root).resolve()
    project.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".candidate-review-", dir=project) as temporary:
        root = Path(temporary)
        for name, content in snapshot.files.items():
            path = root / file_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        validation_job = {**job, "workspace": str((root / snapshot.workspace).relative_to(project))}
        if int(job.get("candidate_contract_version") or 1) >= 2:
            plan = repository._read_capability_plan(validation_job)
            if skill_name not in {entry["name"] for entry in plan["skills"]}:
                raise ValueError("该 Skill Candidate 不在能力拆分计划中")
        directory = root / snapshot.workspace / "compiled_skill" / skill_name
        repository._validate_candidate(validation_job, directory)
        dataset, errors = benchmark_format.read_document(directory / "benchmark.json")
        if errors or dataset is None:
            raise ValueError("Test Dataset is incomplete")
    material_dataset, material_bundle = materialize(dataset, bundle)
    replay_cases = document_cases(skill_name, material_dataset["cases"])
    if any(case["materials"] for case in replay_cases):
        material_dataset["cases"] = replay_cases
        material_bundle["benchmark.json"] = (json.dumps(material_dataset, ensure_ascii=False, indent=2) + "\n").encode()
        material_bundle["BENCHMARK.md"] = dataset_markdown(material_dataset).encode()
    canonical = validate_bundle(skill_name, _canonical_bundle(skill_name, material_bundle), limits)
    candidate = skill_payload(skill_name, canonical)
    candidate["file_changes"] = [{"path": path, "operation": "upsert"} for path in canonical]
    static = validate_candidate_bundle(candidate)
    if not static["passed"]:
        raise ValueError("Skill Candidate 静态校验未通过：" + "；".join(static["errors"]))
    return {
        "bundle": canonical, "artifact_sha256": bundle_tree_sha256(bundle),
        "replay_cases": hydrate(replay_cases, canonical, maximum=limits.max_bytes), "static_validation": static,
        "file_changes": candidate["file_changes"],
    }


def _response(item, *, created, job_id, question_count):
    return {"created": created, "job_id": item["candidate_id"], "skill_name": item["skill_name"],
            "status": item["status"] if item["status"] in {"published", "rejected"} else "candidate",
            "candidate_revision": item["revision"], "review_status": item["status"],
            "dataset_format": DATASET_SCHEMA, "question_count": question_count, "mining_job_id": job_id}


async def submit(engine, body, job_id, submitted_by):
    """Create one fenced, idempotent open candidate; never validate or publish it."""
    if not isinstance(body, dict) or set(body) - {
        "skill_name", "artifact_path", "_accepted_job_revision", "_accepted_files_revision",
    }:
        raise ValueError("candidate submission requires only skill_name and artifact_path")
    accepted = (body.get("_accepted_job_revision"), body.get("_accepted_files_revision"))
    if any(type(value) is not int or value < 0 for value in accepted) or accepted[0] < 1:
        raise ValueError("candidate submission requires the accepted mining snapshot revisions")
    name = validate_skill_name(body.get("skill_name") or "")
    job_id = identity(job_id)
    artifact_path = body.get("artifact_path", "")
    if not isinstance(artifact_path, str):
        raise ValueError("artifact_path must be a string")
    if engine.task is None or engine.task.tenant_id != engine.tenant:
        raise ValueError("candidate submission requires a tenant-bound Worker task")
    await engine.prepare()
    services, tenant = engine.services, engine.tenant
    snapshots = MiningCandidateRepository(services.tx)
    configs = services.extras.get("config_repo") or ConfigRepository(services.tx)
    async with engine.job_lock(job_id):
        async with services.tx.tenant_tx(tenant) as conn:
            await services.tasks.require_current(conn, engine.task)
            config = await load_skills_config(configs, conn)
            snapshot = await snapshots.load(conn, tenant, job_id, name, limits=config.bundle_limits)
            if (snapshot.revision, snapshot.files_revision) != accepted:
                raise MiningRevisionConflict("mining artifacts changed after acceptance; review before resubmitting")
        prepared = await asyncio.to_thread(_prepare, engine.repository, snapshot, name, artifact_path,
                                           config.bundle_limits)
        artifact_hash = prepared["artifact_sha256"]
        candidate_id = "mined-" + hashlib.sha256(
            f"{name}:{artifact_hash}:binding-{BINDING_VERSION}".encode(),
        ).hexdigest()
        repo = CandidateRepository(services.tx)
        library = SkillLibraryRepository(services.tx)
        async with services.tx.tenant_tx(tenant) as conn:
            await services.tasks.require_current(conn, engine.task)
            await snapshots.require_snapshot(conn, tenant, snapshot)
            await lock_key(conn, tenant, f"candidate-skill:{name}")
            existing = await repo.get(conn, candidate_id, for_update=True)
            if existing is not None:
                return _response(existing, created=False, job_id=job_id,
                                 question_count=len(prepared["replay_cases"]))
            # create() revises any live candidate, including one in validation.
            # Submission must not overwrite another proposal or a human's edits.
            if await repo.live_for_skill(conn, name) is not None:
                raise CandidateStateError("该 Skill 已有待评审候选，请先处理已有候选后再提交新版本")
            head = await library.get_skill(conn, name)
            baseline = await library.read_version_bundle(conn, name, head["version"]) if head else {}
            detail = {
                "rationale": (f"SkillMiner 已校验 {len(prepared['replay_cases'])} 条 Test Dataset；"
                              "等待 True Replay 与 Candidate Review。"),
                "source": {"kind": "skillminer", "skill_name": name, "mining_job_id": job_id,
                           "artifact_sha256": artifact_hash, "dataset_format": DATASET_SCHEMA,
                           "question_count": len(prepared["replay_cases"]), "submitted_by": str(submitted_by or ""),
                           "replay_binding_version": BINDING_VERSION,
                           "mining_revision": snapshot.revision, "files_revision": snapshot.files_revision},
                "replay_cases": prepared["replay_cases"], "runtime_validation_policy": copy.deepcopy(DOCUMENT_POLICY),
                "file_changes": prepared["file_changes"], "static_validation": prepared["static_validation"],
                "bundle_diff": diff_skill_bundles(baseline, prepared["bundle"]),
            }
            item = await repo.create(
                conn, tenant_id=tenant, skill_name=name, bundle=prepared["bundle"], limits=config.bundle_limits,
                proposed_action="update" if head else "create", base_version=head["version"] if head else 0,
                detail=detail, candidate_id=candidate_id, actor=str(submitted_by or "skillminer"),
            )
            await services.tasks.require_current(conn, engine.task)
            return _response(item, created=True, job_id=job_id, question_count=len(prepared["replay_cases"]))
