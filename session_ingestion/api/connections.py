"""Named datasource connections use the existing accepted Pull/Worker pipeline."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id, get_current_tenant

from ..adapters._runtime import filters_for, validate_content
from ..adapters._shared.connection_settings import expanded_settings
from ..adapters.repository import AdapterRepository
from ..connections import ConnectionConflict, ConnectionNotFound, ConnectionRepository, freeze_record
from ..http import read_limited_json_body
from . import _accepted_response, _require_tenant_admin


def create_router(services, accept):
    router = APIRouter(tags=["datasource"])
    tx, repository = services.tx, ConnectionRepository(services.tx)

    async def describe(conn, identity):
        result = await repository.freeze(conn, identity)
        version = await AdapterRepository(tx).get_version(
            conn, adapter_id=result["adapter_id"], revision=result["adapter_revision"],
        )
        result["adapter_metadata"] = validate_content(version["source"], result["adapter_id"])
        return result

    @router.get("/api/datasource/connections")
    async def listing(request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return {"connections": [freeze_record(row) for row in await repository.list(conn)]}

    @router.get("/api/datasource/connections/{identity}")
    async def detail(identity: str, request: Request):
        _require_tenant_admin(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await describe(conn, identity)
        except ConnectionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.put("/api/datasource/connections/{identity}")
    async def save(identity: str, request: Request):
        _require_tenant_admin(request)
        raw = await read_limited_json_body(request)
        expected = raw.pop("expected_revision", None)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                record = await repository.save(conn, tenant_id=current_tenant_id(), identity=identity,
                                               expected_revision=expected, document=raw)
                secrets = services.extras.get("secret_repo")
                version = await AdapterRepository(tx).get_version(
                    conn, adapter_id=record["document"]["adapter_id"], revision=record["document"]["adapter_revision"],
                )
                metadata = validate_content(version["source"], record["document"]["adapter_id"])
                settings = expanded_settings(record["document"]["settings"], metadata)
                for reference in settings["secret_refs"].values():
                    if secrets is None or await secrets.describe(conn, secret_ref=reference) is None:
                        raise ValueError("connection Secret Ref does not exist in this tenant")
                return await describe(conn, identity)
        except ConnectionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    async def operation(identity, request, mode):
        _require_tenant_admin(request)
        raw = await read_limited_json_body(request)
        if set(raw) - {"filters", "from_timestamp", "to_timestamp", "max_sessions"}:
            raise HTTPException(400, "expected filters, timestamps and optional max_sessions")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                snapshot = await repository.freeze(conn, identity)
                version = await AdapterRepository(tx).get_version(
                    conn, adapter_id=snapshot["adapter_id"], revision=snapshot["adapter_revision"],
                )
            if not snapshot["enabled"] and mode == "pull":
                raise HTTPException(409, "datasource connection is disabled")
            filters = raw.get("filters", {})
            if not isinstance(filters, dict):
                raise ValueError("filters must be an object")
            fixed_agent = snapshot["default_filters"].get("agent_id")
            if fixed_agent and "agent_id" in filters and filters["agent_id"] != fixed_agent:
                raise ValueError("request cannot override the connection's Agent selection")
            merged = {**snapshot["default_filters"], **filters}
            for key in ("from_timestamp", "to_timestamp"):
                if key in raw:
                    if key in filters and filters[key] != raw[key]:
                        raise ValueError("conflicting timestamp fields")
                    merged[key] = raw[key]
            metadata = validate_content(version["source"], snapshot["adapter_id"])
            if mode != "health":
                merged, _ = filters_for(metadata, merged)
            limit = raw.get("max_sessions", 100)
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError("max_sessions must be 1..1000")
            pull = {"connection": snapshot, "filters": merged, "operation": mode, "max_sessions": limit}
            account = getattr(get_current_tenant(), "account_id", "") or ""
            job = await accept(source="pull", external_id="", user_id="", account_id=account,
                               session={"_pull": pull}, pull=pull, adapter_id=snapshot["adapter_id"],
                               adapter_revision=snapshot["adapter_revision"], kind="session.pull",
                               request_key=request.headers.get("Idempotency-Key", ""))
            return _accepted_response(job)
        except ConnectionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/datasource/connections/{identity}/pull", status_code=202)
    async def pull(identity: str, request: Request):
        return await operation(identity, request, "pull")

    @router.post("/api/datasource/connections/{identity}/test", status_code=202)
    async def health(identity: str, request: Request):
        return await operation(identity, request, "health")

    @router.post("/api/datasource/connections/{identity}/sessions", status_code=202)
    async def preview(identity: str, request: Request):
        return await operation(identity, request, "preview")

    return router
