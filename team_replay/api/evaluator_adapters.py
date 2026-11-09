"""Root-authored evaluator source; selection stays in revisioned Replay config."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

from ..evaluator_adapters import EvaluatorAdapterRepository, validate_evaluator_source
from .common import ERRORS, actor, body, error, root


def create_router(services):
    router = APIRouter(tags=["replay-evaluator-adapter"])
    repo, tx = EvaluatorAdapterRepository(services.tx), services.tx

    @router.get("/api/replay-evaluator-adapter")
    async def describe(request: Request):
        actor(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            available = await repo.list(conn)
        return {"available": available,
                "source_editable": bool(getattr(request.state, "service_root_authenticated", False))}

    @router.get("/api/replay-evaluator-adapter/example")
    async def example(request: Request):
        actor(request)
        filename = "http_evaluator.py"
        path = Path(__file__).parents[1] / "adapters" / "examples" / filename
        source = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return {"file": filename, "code": source, **validate_evaluator_source(source, filename)}

    @router.get("/api/replay-evaluator-adapter/code")
    async def code(request: Request, file: str, revision: int | None = None):
        root(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                source = await repo.source(conn, file, revision)
            return {"file": file, "code": source["source"], "revision": source["revision"],
                    "source_hash": source["source_hash"]}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.put("/api/replay-evaluator-adapter/code")
    async def save(request: Request):
        root(request)
        raw = await body(request)
        if set(raw) != {"file", "code", "expected_revision"}:
            raise HTTPException(400, "expected file, code, expected_revision")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await repo.save(conn, tenant_id=current_tenant_id(), filename=raw["file"],
                                       source=raw["code"], expected_revision=raw["expected_revision"])
        except ERRORS as exc:
            raise error(exc) from exc

    return router
