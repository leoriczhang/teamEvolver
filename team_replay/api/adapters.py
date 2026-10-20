"""PG Replay adapter control plane; Python source requires service Root Key."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

from ..adapters.loader import validate_content
from ..adapters.repository import ReplayAdapterRepository
from ..commands import enqueue
from ..datasets.repository import DatasetNotFound
from .common import ERRORS, accepted, actor, body, error, identity, root


def create_router(services):
    router = APIRouter(tags=["replay-adapter"])
    repo = ReplayAdapterRepository(services.tx)
    tx = services.tx

    @router.get("/api/replay-adapter")
    async def describe(request: Request):
        actor(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            bound = await repo.binding(conn)
            available = await repo.list(conn)
        result = {"tenant_id": current_tenant_id(), "available": available, "configured": bool(bound),
                  "source_editable": bool(getattr(request.state, "service_root_authenticated", False))}
        if bound:
            result.update(file=bound["adapter_id"], revision=bound["revision"],
                          **validate_content(bound["source"], bound["adapter_id"]))
        return result

    @router.put("/api/replay-adapter")
    async def bind(request: Request):
        actor(request)
        raw = await body(request)
        if set(raw) - {"file", "revision"} or not isinstance(raw.get("file"), str):
            raise HTTPException(400, "expected file and optional revision")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                source = await repo.bind(conn, tenant_id=current_tenant_id(), filename=raw["file"],
                                         revision=raw.get("revision"))
            return {"file": raw["file"], "revision": source["revision"] if source else None}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.get("/api/replay-adapter/code")
    async def code(request: Request, file: str = ""):
        root(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                source = await repo.source(conn, file) if file else await repo.binding(conn)
            if not source:
                raise HTTPException(404, "Replay adapter not found")
            return {"file": source["adapter_id"], "code": source["source"], "revision": source["revision"]}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.put("/api/replay-adapter/code")
    async def save(request: Request):
        root(request)
        raw = await body(request)
        if set(raw) != {"file", "code", "expected_revision"}:
            raise HTTPException(400, "expected file, code, expected_revision")
        expected = raw["expected_revision"]
        if expected is not None and (type(expected) is not int or expected < 1):
            raise HTTPException(400, "expected_revision must be a positive integer or null")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await repo.save(conn, tenant_id=current_tenant_id(), filename=raw["file"],
                                       source=raw["code"], expected_revision=expected)
        except ERRORS as exc:
            raise error(exc) from exc

    @router.post("/api/replay-adapter/code/test", status_code=202)
    async def test(request: Request):
        root(request)
        raw = await body(request)
        if set(raw) != {"file", "code", "context", "query"}:
            raise HTTPException(400, "expected explicit file, code, context, query")
        try:
            validate_content(raw["code"], raw["file"])
            context = raw["context"]
            if not isinstance(context, dict) or set(context) - {
                "runtime_type", "skill", "materials", "context_snapshot", "timeout_seconds",
            }:
                raise ValueError("invalid Adapter test context")
            if not isinstance(raw["query"], str) or not 1 <= len(raw["query"].strip()) <= 32000:
                raise ValueError("test query must contain 1..32000 characters")
            if not isinstance(context.get("runtime_type"), str) or not context["runtime_type"]:
                raise ValueError("runtime_type is required")
            if context.get("skill") is not None and not isinstance(context["skill"], dict):
                raise ValueError("skill must be an object or null")
            if not isinstance(context.get("materials", []), list):
                raise ValueError("materials must be an array")
            if not isinstance(context.get("context_snapshot", {}), dict):
                raise ValueError("context_snapshot must be an object")
            timeout = context.get("timeout_seconds", 30)
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise ValueError("test timeout_seconds must be 1..120")
            source_hash = hashlib.sha256(raw["code"].encode()).hexdigest()
            draft_id = "test-" + source_hash + ".py"
            async with tx.tenant_tx(current_tenant_id()) as conn:
                try:
                    source = await repo.source(conn, draft_id)
                except DatasetNotFound:
                    source = await repo.save(
                        conn, tenant_id=current_tenant_id(), filename=draft_id, source=raw["code"],
                        expected_revision=None,
                    )
                pinned_test = {
                    "adapter_id": draft_id, "revision": source["revision"], "source_hash": source_hash,
                    "context": context, "query": raw["query"],
                }
                record = await enqueue(
                    conn, services, tenant_id=current_tenant_id(), domain="skill",
                    run_id=identity(request, "adapter-test"), subject_id=raw["file"],
                    request={"test": pinned_test}, options={"test": pinned_test}, operation="adapter_test",
                )
            return accepted(record)
        except ERRORS as exc:
            raise error(exc) from exc
    return router
