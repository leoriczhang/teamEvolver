"""Source APIs accept external work; inventory reads only tenant PostgreSQL rows."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .. import task_io
from ..source_operations import child, safe_uri, source_inventory
from .context import Context, read_body, translate


def create_router(services):
    router = APIRouter(prefix="/api/mining", tags=["skill-mining-sources"])
    context = Context(services)

    async def enqueue(request, operation, raw):
        settings = await context.settings(request, str(raw.get("account") or ""))
        try:
            if operation not in {"source.list", "source.create", "ov.tree"} and not raw.get("source_id"):
                raise ValueError("source_id 必填")
            if raw.get("source_id"):
                record = await asyncio.to_thread(context.repo().get_source, str(raw["source_id"]))
                await context.settings(request, str(record.get("account") or ""))
                if operation in {"source.content", "source.save", "source.delete"}:
                    child(record, raw.get("uri"))
                if operation == "source.upload":
                    child(record, raw.get("parent_uri") or record["uri"], allow_root=True)
            if operation in {"source.create", "ov.tree"}:
                safe_uri(raw.get("uri") or "viking://resources", allow_root=operation == "ov.tree")
        except (ValueError, KeyError, RuntimeError) as exc:
            raise translate(exc) from exc
        return await task_io.accept(request, services, operation, raw, settings)

    @router.get("/input-sources")
    async def inventory(request: Request):
        settings = await context.settings(request)
        rows = await asyncio.to_thread(context.repo().list_source_records)
        return {"sources": [source_inventory(row) for row in rows
                            if row.get("account") == settings.miner_config["account_id"]]}

    @router.get("/ov-sources", status_code=202)
    async def list_sources(request: Request):
        return await enqueue(request, "source.list", dict(request.query_params))

    @router.post("/ov-sources", status_code=202)
    async def create_source(request: Request):
        return await enqueue(request, "source.create", await read_body(request))

    @router.patch("/ov-sources/{source_id}", status_code=202)
    async def update_source(source_id: str, request: Request):
        return await enqueue(request, "source.update", {**await read_body(request), "source_id": source_id})

    @router.delete("/ov-sources/{source_id}", status_code=202)
    async def remove_source(source_id: str, request: Request):
        return await enqueue(request, "source.remove", {"source_id": source_id})

    @router.get("/input-sources/{source_id}", status_code=202)
    async def source_detail(source_id: str, request: Request):
        return await enqueue(request, "source.detail", {"source_id": source_id})

    @router.get("/ov-source-tree", status_code=202)
    async def source_tree(request: Request, source_id: str):
        return await enqueue(request, "source.tree", {"source_id": source_id})

    @router.get("/ov-tree", status_code=202)
    async def ov_tree(request: Request, account: str = "", uri: str = "viking://resources"):
        return await enqueue(request, "ov.tree", {"account": account, "uri": uri})

    @router.get("/ov-source-content", status_code=202)
    async def source_content(request: Request, source_id: str, uri: str):
        return await enqueue(request, "source.content", {"source_id": source_id, "uri": uri})

    @router.post("/ov-source-content", status_code=202)
    async def save_content(request: Request):
        return await enqueue(request, "source.save", await read_body(request))

    @router.post("/ov-source-upload", status_code=202)
    async def upload(request: Request):
        return await enqueue(request, "source.upload", await read_body(request, maximum=140 * 1024 * 1024))

    @router.delete("/ov-entry", status_code=202)
    async def delete_entry(request: Request):
        return await enqueue(request, "source.delete", await read_body(request))

    return router
