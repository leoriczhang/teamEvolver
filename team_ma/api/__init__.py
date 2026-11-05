"""MA console administration and tenant-key release snapshots."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.security.agent_principal import resolve_agent_principal
from teamEvolver.tenants.registry import current_tenant_id

from ..commands import enqueue
from ..repository import MARepository
from ..schemas import MAConflict, digest, public_config, validate_config


def _admin(request):
    require_tenant_admin(getattr(request.state, "console_user", None))


def _public(row):
    return {**{k: v for k, v in row.items() if k not in {"config", "target_fingerprint"}},
            "config": public_config(row["config"])}


def _operation_id(request, connection_id, kind):
    key = request.headers.get("idempotency-key", "")
    if len(key) > 200:
        raise HTTPException(400, "Idempotency-Key is too long")
    return "ma:" + hashlib.sha256(f"{connection_id}:{kind}:{key}".encode()).hexdigest()[:40] if key else None


async def _body(request):
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "MA configuration exceeds size limit")
    import json
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Expected JSON object") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "Expected JSON object")
    return value


def create_router(services=None, **kwargs):
    router = APIRouter(tags=["managed-agents"])
    if services is None or not services.extras.get("config_repo"):
        return router
    repo = MARepository(services.tx)

    async def require(conn, ident):
        row = await repo.connection(conn, ident)
        if not row:
            raise HTTPException(404, "MA connection not found")
        return row

    async def credentials(conn, body, previous):
        config = dict(body)
        secrets = services.extras.get("secret_repo")
        for key, ref in (("ma_api_key", "ma_secret_ref"), ("tenant_token", "tenant_token_ref")):
            value = config.pop(key, "")
            if value:
                if not isinstance(value, str) or len(value) > 8192:
                    raise HTTPException(400, "Invalid credential")
                if ref == "tenant_token_ref" and not value.startswith("tevt_"):
                    raise HTTPException(400, "A tenant data-plane Key (tevt_) is required")
                if not secrets:
                    raise HTTPException(503, "Credential encryption is not configured")
                config[ref] = (await secrets.store(conn, tenant_id=current_tenant_id(),
                                                   plaintext=value, purpose="managed-agents")).ref
            elif previous and ref in previous:
                config[ref] = previous[ref]
            if config.get(ref):
                if not secrets or not await secrets.describe(conn, secret_ref=config[ref]):
                    raise HTTPException(400, "Credential reference does not belong to this tenant")
        return config

    @router.get("/api/ma/connections")
    async def list_connections(after: str = "", limit: int = 50):
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            rows = await repo.connections(conn, after=after, limit=limit)
        return {"connections": [_public(r) for r in rows],
                "next_after": rows[-1]["connection_id"] if len(rows) == min(limit, 100) else None}

    @router.get("/api/ma/connections/{connection_id}")
    async def get_connection(connection_id: str):
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            row = await require(conn, connection_id)
            cursors = await repo.cursors(conn, connection_id, limit=20)
        return {**_public(row), "sessions": cursors}

    async def save(request, ident=None):
        _admin(request)
        body = await _body(request)
        expected = body.pop("expected_revision", 0)
        if type(expected) is not int or expected < 0:
            raise HTTPException(400, "expected_revision must be a nonnegative integer")
        actor = str((getattr(request.state, "console_user", None) or {}).get("id", ""))
        connection_id = ident or body.pop("connection_id", None) or "ma_" + uuid4().hex
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", connection_id):
            raise HTTPException(400, "Invalid connection ID")
        creation_hash = digest(body)
        operation_id = _operation_id(request, connection_id, "discover") if not ident else None
        try:
            async with services.tx.tenant_tx(current_tenant_id()) as conn:
                if operation_id:
                    existing = await repo.operation(conn, operation_id)
                    if existing:
                        if existing["input"].get("request", {}).get("creation_hash") != creation_hash:
                            raise MAConflict("Idempotency-Key was reused with different connection settings")
                        row = await repo.connection(conn, connection_id, existing["input"]["revision"])
                        job = await enqueue(conn, services, tenant_id=current_tenant_id(),
                                            connection_id=connection_id, kind="ma.discover",
                                            body={"creation_hash": creation_hash}, operation_id=operation_id)
                        return JSONResponse(status_code=202, content=jsonable_encoder(
                            {**job, "connection": _public(row)}))
                previous = await require(conn, ident) if ident else None
                config = await credentials(conn, body, previous["config"] if previous else None)
                config = validate_config({**(previous["config"] if previous else {}), **config})
                row = await repo.save(conn, tenant_id=current_tenant_id(), connection_id=connection_id,
                                      config=config, expected_revision=expected, actor=actor)
                if not previous:
                    job = await enqueue(conn, services, tenant_id=current_tenant_id(), connection_id=connection_id,
                                        kind="ma.discover",
                                        body={"creation_hash": creation_hash}, operation_id=operation_id)
                else:
                    return _public(row)
        except MAConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse(status_code=202, content=jsonable_encoder({**job, "connection": _public(row)}))

    @router.post("/api/ma/connections")
    async def create_connection(request: Request):
        return await save(request)

    @router.put("/api/ma/connections/{connection_id}")
    async def update_connection(connection_id: str, request: Request):
        return await save(request, connection_id)

    async def operation(request, ident, kind):
        _admin(request)
        body = await _body(request)
        if kind == "ma.collect" and set(body) - {"session_ids", "from_timestamp", "to_timestamp"}:
            raise HTTPException(400, "Expected session_ids or time range")
        if kind != "ma.collect" and body:
            raise HTTPException(400, "This operation takes no fields")
        if "session_ids" in body and (not isinstance(body["session_ids"], list)
                                     or not 1 <= len(body["session_ids"]) <= 100
                                     or any(not isinstance(v, str) or not v for v in body["session_ids"])):
            raise HTTPException(400, "session_ids must contain 1–100 IDs")
        for field in ("from_timestamp", "to_timestamp"):
            if field in body:
                from datetime import datetime
                try:
                    if not isinstance(body[field], str) or datetime.fromisoformat(body[field]).tzinfo is None:
                        raise ValueError()
                except ValueError:
                    raise HTTPException(400, "Expected timezone-aware ISO timestamp") from None
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            await require(conn, ident)
            try:
                result = await enqueue(conn, services, tenant_id=current_tenant_id(), connection_id=ident, kind=kind,
                                       body=body, operation_id=_operation_id(request, ident, kind))
            except (MAConflict, ValueError) as exc:
                raise HTTPException(409, str(exc)) from None
        return JSONResponse(status_code=202, content=result)

    @router.post("/api/ma/connections/{connection_id}/discover")
    async def discover(connection_id: str, request: Request):
        return await operation(request, connection_id, "ma.discover")

    @router.post("/api/ma/connections/{connection_id}/collect")
    async def collect(connection_id: str, request: Request):
        return await operation(request, connection_id, "ma.collect")

    @router.post("/api/ma/connections/{connection_id}/reconcile")
    async def reconcile(connection_id: str, request: Request):
        return await operation(request, connection_id, "ma.reconcile")

    @router.get("/api/ma/connections/{connection_id}/resources")
    async def resources(connection_id: str, after: str = "", limit: int = 100, kind: str = "skill"):
        if kind not in {"agent", "skill"}:
            raise HTTPException(400, "Expected agent or skill")
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            await require(conn, connection_id)
            rows = await repo.resources(conn, connection_id, after=after, limit=limit, kind=kind)
        keys = {"id", "name", "version", "source_version", "team_version", "tree_sha256", "agents", "status", "drift"}
        return {"resources": [{**row, "record": {k: v for k, v in row["record"].items() if k in keys}}
                              for row in rows],
                "next_after": rows[-1]["logical_name"] if len(rows) == min(max(limit, 1), 100) else None}

    @router.put("/api/ma/connections/{connection_id}/resources/{skill_name}/bindings")
    async def bindings(connection_id: str, skill_name: str, request: Request):
        _admin(request)
        body = await _body(request)
        if (set(body) != {"expected_revision", "agent_ids"} or not isinstance(body["agent_ids"], list)
                or type(body["expected_revision"]) is not int or body["expected_revision"] < 0
                or len(body["agent_ids"]) > 20
                or any(not isinstance(v, str) or not v for v in body["agent_ids"])
                or len(set(body["agent_ids"])) != len(body["agent_ids"])):
            raise HTTPException(400, "Expected expected_revision and agent_ids")
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            await require(conn, connection_id)
            agents = {r["logical_name"] for r in await repo.all_resources(conn, connection_id, kind="agent")}
            if not set(body["agent_ids"]) <= agents:
                raise HTTPException(400, "Unknown target Agent")
            rows = await repo.all_resources(conn, connection_id, kind="skill")
            existing = next((r for r in rows if r["logical_name"] == skill_name), None)
            record = {**(existing["record"] if existing else {}), "name": skill_name, "agents": body["agent_ids"]}
            try:
                if not existing and body["expected_revision"] == 0:
                    await repo.create_binding(conn, tenant_id=current_tenant_id(), connection_id=connection_id,
                                              name=skill_name, record=record)
                else:
                    await repo.bind(conn, tenant_id=current_tenant_id(), connection_id=connection_id, kind="skill",
                                    name=skill_name, record=record, expected_revision=body["expected_revision"])
            except MAConflict as exc:
                raise HTTPException(409, str(exc)) from None
        return {"saved": True}

    @router.get("/api/ma/connections/{connection_id}/releases")
    async def releases(connection_id: str, limit: int = 50):
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            await require(conn, connection_id)
            rows = await repo.releases(conn, connection_id, limit=limit)
        # Config snapshots can contain scripts; only release/public version facts are returned.
        return {"releases": [{**{k: v for k, v in r.items() if k != "record"},
                              "record": _release_public(r["record"])} for r in rows]}

    @router.get("/sync/ma/{connection_id}")
    async def sync_release(connection_id: str, request: Request, user_id: str = ""):
        principal = resolve_agent_principal(request, services.extras.get("config"), user_id)
        async with services.tx.tenant_tx(principal.tenant_id) as conn:
            await require(conn, connection_id)
            release = await repo.applied(conn, connection_id)
        if not release:
            raise HTTPException(503, "No fully applied MA release is available")
        etag = '"' + release["release_id"] + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
        validators = {v.strip().removeprefix("W/") for v in request.headers.get("if-none-match", "").split(",")}
        if etag in validators:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=jsonable_encoder(_release_public(release["record"])), headers=headers)

    return router


def _release_public(record):
    keys = ("release_id", "manifest_hash", "entry_agent_id", "entry_agent_version", "environment_id",
            "environment_hash", "agents", "skills", "connection_revision", "runtime_tree_sha256", "error", "drift")
    return {k: record[k] for k in keys if k in record}
