"""Explicit Skill/Memory Replay inputs and tenant scoped immutable run queries."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

from .._util import stable_hash
from ..commands import enqueue
from ..config import run_options
from ..contracts import ReplaySpec, ReplayTreatment
from ..datasets.repository import DatasetRepository
from ..datasets.schema import DATASET_SCHEMA, normalize_document
from ..repository import ReplayRepository
from .common import ERRORS, accepted, actor, body, error, identity
from .datasets import present_run


def create_router(services):
    router = APIRouter(tags=["replay"])
    repo = ReplayRepository(services.tx)
    tx = services.tx

    @router.post("/api/replays/{domain}", status_code=202)
    async def start(domain: str, request: Request):
        actor(request)
        raw = await body(request)
        if domain not in {"skill", "memory"}:
            raise HTTPException(400, "Replay domain must be skill or memory")
        required = {"subject_id", "dataset", "baseline", "candidate", "runtime"}
        if not required <= set(raw) or set(raw) - required - {"options"}:
            raise HTTPException(400, "expected subject_id, dataset, baseline, candidate, runtime and optional options")
        try:
            options = run_options(raw.get("options", {}))
            document = normalize_document(raw["dataset"])
            if domain == "memory":
                for treatment in (raw["baseline"], raw["candidate"]):
                    if not isinstance(treatment, dict) or set(treatment) != {"revision", "content", "uri"}:
                        raise ValueError("Memory treatment requires revision, uri and content")
                    if not isinstance(treatment["content"], str) or not isinstance(treatment["revision"], str):
                        raise ValueError("Memory revision and content must be text")
                    if not treatment["revision"] or not isinstance(treatment["uri"], str):
                        raise ValueError("Memory revision and URI are required")
                if raw["baseline"]["uri"] != raw["candidate"]["uri"]:
                    raise ValueError("Memory branches must treat the same URI")
            if not isinstance(raw["runtime"], dict) or not isinstance(raw["runtime"].get("type"), str):
                raise ValueError("runtime.type is required")
            if not isinstance(raw["subject_id"], str) or not raw["subject_id"]:
                raise ValueError("subject_id is required")
            run_id = identity(request, domain)
            def treatment(value):
                if domain == "memory":
                    sha = hashlib.sha256(value["content"].encode()).hexdigest()
                    return ReplayTreatment("memory", value["revision"], value, content_hash=sha)
                if not isinstance(value, dict) or set(value) != {"revision", "payload"}:
                    raise ValueError("Skill treatment requires revision and immutable payload")
                return ReplayTreatment("skill_bundle", str(value["revision"]), value["payload"],
                                       content_hash=stable_hash(value["payload"]))
            spec = ReplaySpec(
                run_id=run_id, subject_kind=domain, subject_id=raw["subject_id"],
                subject_ids=tuple(ref["skill_id"] for ref in document["skills"]),
                dataset_format=DATASET_SCHEMA, cases=tuple(document["cases"]),
                baseline=treatment(raw["baseline"]), candidate=treatment(raw["candidate"]),
                runtime=raw["runtime"], limits=options, metadata={"dataset_hash": stable_hash(document)},
            )
            async with tx.tenant_tx(current_tenant_id()) as conn:
                # Inline materials are fixed and verified at acceptance.
                material_repo = DatasetRepository(tx)
                for case in document["cases"]:
                    case["materials"] = [await material_repo.material(conn, material) for material in case["materials"]]
                spec = replace(spec, cases=tuple(document["cases"]))
                record = await enqueue(conn, services, tenant_id=current_tenant_id(), domain=domain,
                                       run_id=run_id, subject_id=raw["subject_id"], request=raw, spec=spec,
                                       options=options)
            return accepted(record)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/replays/{domain}/{run_id}")
    async def detail(domain: str, run_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await present_run(conn, services, domain, await repo.get(conn, domain, run_id))
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/replays/{domain}/{run_id}/input")
    async def fixed_input(domain: str, run_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                row = await repo.get(conn, domain, run_id)
            return {"spec": row["input"].get("spec"), "job": row["input"].get("job"), "input_hash": row["input_hash"]}
        except ERRORS as exc:
            raise error(exc) from exc
    return router
