"""Miner routes; APIs accept persistent work and share upstream task identities."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import TenantConfigMissing
from teamEvolver.openviking import OpenVikingError
from teamEvolver.tenants.registry import current_tenant_id, effective_config, get_current_tenant

from ..config import load
from ..openviking import clients, ensure_workspace
from ..openviking import request as ov_request
from ..schemas import MAX_TOTAL_BYTES, task_id, uploads, uri
from ..state import MinerState, digest


def create_router(services=None, **kwargs) -> APIRouter:
    router = APIRouter(prefix="/api/knowledge-mining", tags=["miner"])
    if services is None or not services.extras.get("config_repo"):
        return router

    async def settings(request, account=""):
        require_tenant_admin(getattr(request.state, "console_user", None))
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            try:
                value, revision = await load(services.extras["config_repo"], conn)
            except TenantConfigMissing as exc:
                raise HTTPException(503, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(503, str(exc)) from exc
        cfg = services.extras.get("config")
        if cfg:
            cfg = effective_config(None, get_current_tenant(), cfg)
        selected = value["account_id"] or getattr(cfg, "sharing_viking_account", "")
        if not selected or (account and account != selected):
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
        return {**value, "account_id": selected}, revision

    async def body(request, maximum=MAX_TOTAL_BYTES * 4 // 3 + 1024 * 1024):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > maximum:
                raise HTTPException(413, "Miner request exceeds size budget")
        try:
            value = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise HTTPException(400, "invalid Miner JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "body must be an object")
        return value

    async def accept(request, raw, payload, config_revision):
        tenant = current_tenant_id()
        key = request.headers.get("Idempotency-Key", "")
        if len(key) > 200:
            raise HTTPException(400, "Idempotency-Key too long")
        rid = "miner:" + (digest([key, payload["operation"]]) if key else uuid.uuid4().hex)
        state = MinerState(services.tx, tenant)
        async with services.tx.tenant_tx(tenant) as conn:
            await state.lock(conn, rid)
            prior = await state.get(conn, "payload", rid)
            if prior:
                if prior["request_hash"] != digest(raw):
                    raise HTTPException(409, "Idempotency-Key reused with different request")
            else:
                payload["request_hash"] = digest(raw)
                await state.put(conn, "payload", rid, payload)
                await services.tasks.enqueue(
                    conn, tenant_id=tenant, task_id=rid, kind="miner.track_compile",
                    subject_type="miner", subject_id=payload["settings"]["target_uri"],
                    payload_ref=rid, payload_hash=digest(payload), config_revision=config_revision, dedupe_key=rid,
                )
        return {"ok": True, "job_id": rid, "status": "queued", "status_url": "/api/jobs/" + rid,
                "account": payload["settings"]["account_id"]}

    @router.get("/config")
    async def config(request: Request, account: str = ""):
        value, revision = await settings(request, account)
        return {**value, "revision": revision, "accounts": [{"account_id": value["account_id"]}]}

    @router.get("/capabilities")
    async def capabilities(request: Request, account: str = ""):
        value, _ = await settings(request, account)
        client, compiler = await clients(services, value)
        try:
            result = await compiler.capabilities()
            state = MinerState(services.tx, current_tenant_id())
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                watermark = await state.get(conn, "watermark", state.scope(value))
            return {"account": value["account_id"], "capabilities": result.data,
                    "last_compile_time": (watermark or {}).get("last_compile_time")}
        finally:
            await client.aclose()

    @router.post("/tasks", status_code=202)
    async def start(request: Request):
        raw = await body(request, 128 * 1024)
        value, revision = await settings(request, str(raw.get("account") or ""))
        if set(raw) - {"account", "source_uri", "target_uri", "skill_uri", "instruction", "last_compile_time"}:
            raise HTTPException(400, "unsupported compile request fields")
        for key in ("source_uri", "target_uri", "skill_uri"):
            if raw.get(key, value[key]) != value[key]:
                raise HTTPException(400, "Compile uses configured workspace roots and Skill")
        instruction = str(raw.get("instruction") or "")
        if len(instruction) > 4000:
            raise HTTPException(400, "instruction exceeds 4000 characters")
        state = MinerState(services.tx, current_tenant_id())
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            watermark = await state.get(conn, "watermark", state.scope(value)) or {}
        compile_body = {"from": [value["source_uri"]], "to": value["target_uri"], "skill": value["skill_uri"]}
        if instruction:
            compile_body["instruction"] = instruction
        last = raw.get("last_compile_time") or watermark.get("last_compile_time")
        if last:
            try:
                parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(400, "last_compile_time must be ISO time") from exc
            compile_body["args"] = {"last_compile_time": parsed.isoformat()}
        return await accept(request, raw, {"settings": value, "operation": "compile", "compile": compile_body,
                                          "started_at": datetime.now(timezone.utc).isoformat()}, revision)

    @router.post("/upload", status_code=202)
    async def upload(request: Request):
        raw = await body(request)
        value, revision = await settings(request, str(raw.get("account") or ""))
        try:
            kind = raw.get("kind", "source")
            if kind not in {"source", "wiki"}:
                raise ValueError("invalid upload kind")
            root = value["source_uri" if kind == "source" else "target_uri"]
            directory = uri(raw.get("directory_uri") or raw.get("target_uri") or root, root)
            files = uploads(raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload = {"settings": value, "operation": "upload", "directory": directory,
                   "files": [{"path": path, "content_b64": base64.b64encode(content).decode()}
                             for path, content in files]}
        return await accept(request, raw, payload, revision)

    @router.post("/graph", status_code=202)
    async def graph(request: Request):
        raw = await body(request, 128 * 1024)
        value, revision = await settings(request, str(raw.get("account") or ""))
        return await accept(request, raw, {"settings": value, "operation": "graph"}, revision)

    @router.get("/jobs/{job_id}/result")
    async def result(job_id: str, request: Request):
        await settings(request)
        state = MinerState(services.tx, current_tenant_id())
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            value = await state.get(conn, "result", job_id)
        if value is None:
            raise HTTPException(404, "Miner result not available")
        return value

    async def upstream(request, identifier, account=""):
        value, _ = await settings(request, account)
        state = MinerState(services.tx, current_tenant_id())
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            receipt = await state.get(conn, "upstream", [value["account_id"], identifier])
        if receipt is None:
            raise HTTPException(404, "Miner task not found in tenant")
        client, compiler = await clients(services, value)
        return value, client, compiler, receipt

    @router.get("/tasks")
    async def tasks(request: Request, account: str = ""):
        value, _ = await settings(request, account)
        client, compiler = await clients(services, value)
        try:
            result = await compiler.list_tasks(params={"limit": 100})
            rows = (result.data.get("tasks", result.data.get("items", []))
                    if isinstance(result.data, dict) else result.data)
            state = MinerState(services.tx, current_tenant_id())
            visible = []
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                for row in rows or []:
                    if await state.get(conn, "upstream", [value["account_id"], row.get("task_id", row.get("id"))]):
                        visible.append(row)
            return {"account": value["account_id"], "tasks": visible}
        finally:
            await client.aclose()

    @router.get("/tasks/{identifier}")
    async def task(request: Request, identifier: str, account: str = ""):
        try:
            task_id(identifier)
            value, client, compiler, receipt = await upstream(request, identifier, account)
            try:
                result = await compiler.status(identifier)
                return {"account": value["account_id"], "task": {**result.result, "status": result.state.value},
                        "job_id": receipt["job_id"]}
            finally:
                await client.aclose()
        except OpenVikingError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/tasks/{identifier}/cancel")
    async def cancel(request: Request, identifier: str):
        raw = await body(request, 128 * 1024)
        try:
            task_id(identifier)
            value, client, compiler, receipt = await upstream(request, identifier, str(raw.get("account") or ""))
            try:
                async with services.tx.tenant_tx(current_tenant_id()) as conn:
                    await services.tasks.request_cancel(conn, task_id=receipt["job_id"])
                result = await compiler.cancel(identifier)
                return {"account": value["account_id"], "task": {**result.result, "status": result.state.value}}
            finally:
                await client.aclose()
        except OpenVikingError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/tree")
    async def tree(request: Request, account: str = "", kind: str = "source"):
        value, _ = await settings(request, account)
        if kind not in {"source", "wiki"}:
            raise HTTPException(400, "invalid tree kind")
        client, _ = await clients(services, value)
        try:
            params = {
                "uri": value["source_uri" if kind == "source" else "target_uri"],
                "node_limit": 10000,
                "level_limit": 20,
            }
            try:
                data = await ov_request(client, "GET", "/api/v1/fs/tree", params=params)
            except OpenVikingError as exc:
                if exc.status_code != 404:
                    raise
                await ensure_workspace(client, value)
                data = await ov_request(client, "GET", "/api/v1/fs/tree", params=params)
            return {"account": value["account_id"], "kind": kind, "tree": data}
        except OpenVikingError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        finally:
            await client.aclose()

    @router.get("/wiki-roots")
    async def wiki_roots(request: Request, account: str = ""):
        value, _ = await settings(request, account)
        return {"roots": [{"uri": value["target_uri"], "name": value["target_uri"].rsplit("/", 1)[-1]}]}

    @router.get("/content")
    async def content(request: Request, uri_value: str = "", account: str = "", kind: str = "source"):
        value, _ = await settings(request, account)
        raw_uri = request.query_params.get("uri") or uri_value
        if kind not in {"source", "wiki"}:
            raise HTTPException(400, "invalid content kind")
        try:
            target = uri(raw_uri, value["source_uri" if kind == "source" else "target_uri"], allow_root=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        client, _ = await clients(services, value)
        try:
            data = await ov_request(client, "GET", "/api/v1/content/read", params={"uri": target})
            return {"uri": target, "content": data}
        finally:
            await client.aclose()

    @router.post("/content", status_code=202)
    async def save_content(request: Request):
        raw = await body(request)
        value, revision = await settings(request, str(raw.get("account") or ""))
        kind = raw.get("kind", "source")
        if kind not in {"source", "wiki"}:
            raise HTTPException(400, "invalid content kind")
        root = value["source_uri" if kind == "source" else "target_uri"]
        try:
            target = uri(raw.get("uri"), root, allow_root=False)
            text = raw.get("content")
            if not isinstance(text, str) or len(text.encode()) > 2*1024*1024:
                raise ValueError("content exceeds 2 MiB")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload = {"settings": value, "operation": "upload", "directory": target.rsplit("/", 1)[0],
                   "files": [{"path": target.rsplit("/", 1)[-1],
                              "content_b64": base64.b64encode(text.encode()).decode()}]}
        return await accept(request, raw, payload, revision)

    @router.delete("/entry", status_code=202)
    async def delete(request: Request, account: str = "", kind: str = "source", uri_value: str = ""):
        value, revision = await settings(request, account)
        if kind not in {"source", "wiki"}:
            raise HTTPException(400, "invalid entry kind")
        try:
            target = uri(request.query_params.get("uri") or uri_value,
                         value["source_uri" if kind == "source" else "target_uri"], allow_root=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return await accept(request, {"uri": target},
                            {"settings": value, "operation": "delete", "uri": target}, revision)
    return router
