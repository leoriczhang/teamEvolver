"""PG Skill Workbench: shared versions, draft bundles and asynchronous Replay."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from team_replay.commands import enqueue
from team_replay.config import run_options
from team_replay.datasets.repository import DatasetRepository
from team_replay.datasets.schema import normalize_document
from team_replay.datasets.selection import select_cases
from team_replay.queries import present_run
from team_replay.repository import ReplayRepository
from teamEvolver.tenants.registry import current_tenant_id

from ..config import load_skills_config
from ..library.bundle import bundle_tree_sha256, encode_bundle_payload
from ..library.editor import validate_skill_name
from ..library.repository import SkillLibraryError, SkillLibraryRepository
from ..workbench.experiments import draft_bundle, prepare_spec
from .workbench_http import ERRORS, actor, body, error, identity


def file_payload(path, data):
    sha = hashlib.sha256(data).hexdigest()
    result = {"path": path, "size": len(data), "sha256": sha, "editable": False}
    try:
        text = data.decode()
        if "\0" not in text and len(data) <= 2 * 1024 * 1024:
            result.update(content=text, editable=True)
    except UnicodeDecodeError:
        pass
    return result


def safe_path(path):
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or path.startswith("/"):
        raise ValueError("unsafe Skill file path")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in path.split("/")):
        raise ValueError("unsafe Skill file path")
    return path


def create_router(services):
    router = APIRouter(tags=["skill-workbench"])
    tx = services.tx
    library = SkillLibraryRepository(tx)
    datasets = DatasetRepository(tx)
    runs = ReplayRepository(tx)
    errors = (*ERRORS, SkillLibraryError)

    @router.post("/api/replay-lab/datasets/synthesize", status_code=202)
    async def synthesize(request: Request):
        actor(request)
        raw = await body(request)
        if not isinstance(raw.get("skill_id"), str) or set(raw) - {"skill_id", "session_ids"}:
            raise HTTPException(400, "expected skill_id and optional session_ids")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                head, bundle = await library.read_current_bundle(conn, validate_skill_name(raw["skill_id"]))
                cases = await select_cases(conn, services, {"session_ids": raw["session_ids"]} if raw.get("session_ids")
                                           else {"filters": {"skill": raw["skill_id"]}})
                if len(cases) > 30:
                    raise ValueError("select at most 30 Sessions for synthesis")
                run_id = identity(request, "synthesis")
                fixed = {"document": {"dataset_id": "", "cases": cases}, "document_hash": "",
                         "skill_id": raw["skill_id"], "bundle": encode_bundle_payload(bundle),
                         "skill_revision": head["version"]}
                record = await enqueue(
                    conn, services, tenant_id=current_tenant_id(), domain="dataset", run_id=run_id,
                    subject_id=raw["skill_id"], request=raw, dataset=fixed, operation="synthesis",
                )
                return {"job_id": run_id, "status_url": f"/api/jobs/{run_id}",
                        "result_url": f"/api/replay-lab/jobs/{run_id}/result", "run_id": record["run_id"]}
        except errors as exc:
            raise error(exc) from exc

    @router.get("/api/replay-lab/jobs/{job_id}/result")
    async def synthesis_result(job_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                record = await runs.get(conn, "dataset", job_id)
            return {"result": record["result"]}
        except errors as exc:
            raise error(exc) from exc

    @router.get("/api/replay-lab/datasets")
    async def dataset_list(skill_id: str = ""):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await datasets.list(conn, skill_id=skill_id)
            return {"datasets": [await datasets.get(conn, row["dataset_id"]) for row in rows], "total": len(rows)}

    @router.post("/api/replay-lab/datasets")
    async def save_dataset(request: Request):
        actor(request)
        raw = await body(request)
        files = raw.pop("files", None)
        expected = raw.pop("expected_revision", None)
        try:
            document = normalize_document(raw, default_dataset_id=datasets.new_id(), allow_empty=True)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await datasets.save(conn, tenant_id=current_tenant_id(), document=document,
                                           files=files, expected_revision=expected)
        except errors as exc:
            raise error(exc) from exc

    @router.post("/api/replay-lab/datasets/{dataset_id}/clone")
    async def clone_dataset(dataset_id: str, request: Request):
        actor(request)
        raw = await body(request)
        if raw:
            raise HTTPException(400, "clone does not accept identity or Skill overrides")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await datasets.freeze(conn, dataset_id)
                document = frozen["document"]
                document["dataset_id"] = datasets.new_id()
                document["name"] = document["name"][:115] + " 副本"
                document["metadata"].update(read_only=False)
                return await datasets.save(conn, tenant_id=current_tenant_id(), document=document)
        except errors as exc:
            raise error(exc) from exc

    @router.delete("/api/replay-lab/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await datasets.delete(conn, tenant_id=current_tenant_id(), dataset_id=dataset_id)
            return {"deleted": True}
        except errors as exc:
            raise error(exc) from exc

    async def index(conn):
        rows = await library.list_skills(conn)
        return {"name": "团队 Skill", "root": "团队 Skill", "source": {"backend": "postgres"},
                "skills": [{**row, "files": [f["path"] for f in row["files"]],
                            "file_count": len(row["files"])} for row in rows]}

    @router.get("/api/replay-lab/workspace")
    async def workspace():
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return await index(conn)

    @router.get("/api/replay-lab/skills")
    async def skills():
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return {**await index(conn), "skill_backend": "postgres", "delivery_mode": "pull"}

    @router.get("/api/replay-lab/skills/{name}")
    async def skill(name: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                head, bundle = await library.read_current_bundle(conn, validate_skill_name(name))
            return {**head, "skill_md": bundle["SKILL.md"].decode(), "files": list(bundle),
                    "file_count": len(bundle), "sha256": hashlib.sha256(bundle["SKILL.md"]).hexdigest()}
        except errors as exc:
            raise error(exc) from exc

    @router.get("/api/replay-lab/workspace/{name}/file")
    async def read(name: str, path: str):
        try:
            path = safe_path(path)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                head, bundle = await library.read_current_bundle(conn, validate_skill_name(name))
            if path not in bundle:
                raise HTTPException(404, "Skill file not found")
            return {**file_payload(path, bundle[path]), "version": head["version"]}
        except errors as exc:
            raise error(exc) from exc

    @router.put("/api/replay-lab/workspace/{name}/file")
    async def write(name: str, request: Request):
        reviewer = actor(request)
        raw = await body(request)
        if set(raw) != {"path", "content", "expected_sha256"} or not isinstance(raw["content"], str):
            raise HTTPException(400, "expected path, content and expected_sha256")
        try:
            name = validate_skill_name(name)
            path = safe_path(raw["path"])
            data = raw["content"].encode()
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("file exceeds 2 MiB")
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(services.extras["config_repo"], conn)
                head, bundle = await library.read_current_bundle(conn, name)
                actual = hashlib.sha256(bundle[path]).hexdigest() if path in bundle else None
                if actual != raw["expected_sha256"]:
                    raise HTTPException(409, "Skill file changed; reload before saving")
                bundle[path] = data
                commit = await library.commit_bundle(
                    conn, tenant_id=current_tenant_id(), name=name, bundle=bundle,
                    mutation_id=identity(request, "file"),
                    limits=config.bundle_limits, actor=reviewer, source="workbench", base_version=head["version"],
                )
            return {**file_payload(path, data), "version": commit.version, "cloud": {"synced": True}}
        except errors as exc:
            raise error(exc) from exc

    @router.post("/api/replay-lab/skills/{name}/save")
    async def save_skill(name: str, request: Request):
        actor(request)
        raw = await body(request)
        if set(raw) != {"candidate_skill_md", "expected_sha256"}:
            raise HTTPException(400, "expected candidate_skill_md and expected_sha256")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(services.extras["config_repo"], conn)
                head, bundle = await library.read_current_bundle(conn, name)
                if hashlib.sha256(bundle["SKILL.md"]).hexdigest() != raw["expected_sha256"]:
                    raise HTTPException(409, "Skill changed; reload before saving")
                bundle = draft_bundle(name, bundle, raw["candidate_skill_md"])
                commit = await library.commit_bundle(
                    conn, tenant_id=current_tenant_id(), name=name, bundle=bundle,
                    mutation_id=identity(request, "save"),
                    limits=config.bundle_limits, actor=actor(request), source="workbench", base_version=head["version"],
                )
            return commit.to_response()
        except errors as exc:
            raise error(exc) from exc

    @router.post("/api/replay-lab/runs", status_code=202)
    async def replay(request: Request):
        actor(request)
        raw = await body(request)
        # Current request names identify assets explicitly; Dataset documents
        # themselves never accept the former flat skill_name/items format.
        required = {"skill_id", "dataset_id", "candidate_skill_md"}
        if not required <= set(raw) or set(raw) - required - {"candidate_files", "timeout_seconds", "max_interactions"}:
            raise HTTPException(400, "expected skill_id, dataset_id, candidate_skill_md and optional files/limits")
        try:
            options = run_options({k: raw[k] for k in ("timeout_seconds", "max_interactions") if k in raw})
            run_id = identity(request, "workbench")
            async with tx.tenant_tx(current_tenant_id()) as conn:
                frozen = await datasets.freeze(conn, raw["dataset_id"])
                bundles, versions = {}, {}
                for ref in frozen["document"]["skills"]:
                    peer = ref["skill_id"]
                    head, bundle = await library.read_current_bundle(conn, peer)
                    version = ref.get("revision", head["version"])
                    if type(version) is not int or version < 1:
                        raise ValueError("Skill revision must be a positive integer")
                    if version != head["version"]:
                        bundle = await library.read_version_bundle(conn, peer, version)
                    if ref.get("content_hash") and ref["content_hash"] != bundle_tree_sha256(bundle):
                        raise ValueError("Dataset Skill content hash does not match its version")
                    bundles[peer], versions[peer] = bundle, version
                spec = prepare_spec(
                    run_id=run_id, skill_id=raw["skill_id"], dataset=frozen["execution_document"],
                    bundles=bundles, versions=versions, candidate_skill_md=raw["candidate_skill_md"],
                    candidate_files=raw.get("candidate_files"), options=options, dataset_hash=frozen["document_hash"],
                )
                record = await enqueue(
                    conn, services, tenant_id=current_tenant_id(), domain="skill", run_id=run_id,
                    subject_id=raw["skill_id"], request=raw, spec=spec, dataset=frozen, options=options,
                )
                return await present_run(conn, services, "skill", record)
        except errors as exc:
            raise error(exc) from exc

    @router.get("/api/replay-lab/runs")
    async def history(skill_id: str = "", limit: int = 100):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            records = await runs.list(conn, "skill", subject_id=skill_id, limit=limit)
            return {"runs": [await present_run(conn, services, "skill", record) for record in records]}

    @router.get("/api/replay-lab/runs/{run_id}")
    async def result(run_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                record = await runs.get(conn, "skill", run_id)
                return await present_run(conn, services, "skill", record)
        except errors as exc:
            raise error(exc) from exc
    return router
