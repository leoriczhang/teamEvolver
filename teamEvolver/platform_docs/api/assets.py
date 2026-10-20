"""Platform assets API."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ...admin.users import _request_user
from ...storage import is_not_found_error
from ...tenants.registry import current_tenant_id, effective_config, get_current_tenant
from ..assets import (
    _MAX_CONTENT_BYTES,
    _asset_uri,
    _collect_source_entries,
    _parse_asset_uri,
    _platform_sources,
    _PlatformAssetSource,
    _source_payload,
)


def create_router(bootstrap, services=None):
    app = APIRouter(tags=["platform-assets"])

    def sources_for(config, tenant):
        if services is None or not config.storage_pg_enabled:
            return _platform_sources(config, tenant)
        from ...storage.postgres import PostgresObjectStore
        return [_PlatformAssetSource(
            source_id="artifacts", label="共享任务制品", description="Session 与 Memory 的不可变任务 payload",
            backend="postgres", configured_backend="postgres", bucket=PostgresObjectStore(services.tx, tenant),
            key_prefix="", families={"ingest": "接入原文", "session": "Session 分阶段结果",
                                    "memory": "Memory Compile 结果"})]


    @app.get("/api/platform-assets/tree")
    async def platform_assets_tree(request: Request):
        from ...admin.authorization import require_tenant_admin

        require_tenant_admin(_request_user(request))
        tenant_id = current_tenant_id()
        config = effective_config(None, get_current_tenant(), bootstrap)
        try:
            sources = sources_for(config, tenant_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"platform storage is unavailable: {exc}",
            ) from exc

        async def collect(source: _PlatformAssetSource):
            try:
                entries, family_counts, truncated = await asyncio.to_thread(
                    _collect_source_entries,
                    source,
                )
                payload = _source_payload(
                    source,
                    object_count=sum(family_counts.values()),
                    family_counts=family_counts,
                    truncated=truncated,
                )
                return entries, payload
            except Exception as exc:  # noqa: BLE001
                return [], _source_payload(source, error=str(exc))

        collected = await asyncio.gather(*(collect(source) for source in sources))
        entries = [entry for source_entries, _ in collected for entry in source_entries]
        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "read_only": True,
                "entries": entries,
                "sources": [payload for _, payload in collected],
            }
        )

    @app.get("/api/platform-assets/content")
    async def platform_asset_content(
        request: Request,
        uri: str = Query(...),
    ):
        from ...admin.authorization import require_tenant_admin

        require_tenant_admin(_request_user(request))
        tenant_id = current_tenant_id()
        config = effective_config(None, get_current_tenant(), bootstrap)
        try:
            sources = sources_for(config, tenant_id)
            source, relative_key = _parse_asset_uri(sources, uri)
            physical_key = f"{source.key_prefix}{relative_key}"
            body = await asyncio.to_thread(
                lambda: source.bucket.get_object(physical_key).read()
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            if is_not_found_error(exc):
                raise HTTPException(
                    status_code=404,
                    detail="platform asset not found",
                ) from exc
            raise HTTPException(
                status_code=503,
                detail=f"platform storage is unavailable: {exc}",
            ) from exc
        if len(body) > _MAX_CONTENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="platform asset preview is limited to 4 MB",
            )
        try:
            content = body.decode("utf-8")
            is_text = True
        except UnicodeDecodeError:
            content = ""
            is_text = False
        top = relative_key.split("/", 1)[0]
        times = _artifact_times(content)
        if services and config.storage_pg_enabled:
            async with services.tx.tenant_tx(tenant_id) as conn:
                synced = await conn.fetchval(
                    f"SELECT updated_at FROM {services.tx.pool.schema}.storage_objects WHERE key=$1",
                    relative_key)
            times["ingested_at"] = synced.isoformat() if synced else None
        return JSONResponse(
            content={
                "uri": _asset_uri(source, relative_key),
                "key": relative_key,
                "name": relative_key.rsplit("/", 1)[-1],
                "source_id": source.source_id,
                "source_label": source.label,
                "backend": source.backend,
                "backend_label": source.backend_label,
                "purpose": source.families.get(top, ""),
                "size": len(body),
                "is_text": is_text,
                "content": content,
                **times,
            }
        )

    return app


def _artifact_times(content):
    """Expose independently recorded event and ingestion timestamps."""
    try:
        document = json.loads(content)
    except (ValueError, TypeError):
        return {"business_time": None, "ingested_at": None}
    if not isinstance(document, dict):
        return {"business_time": None, "ingested_at": None}
    if isinstance(document.get("session"), dict):
        document = document["session"]
    return {
        "business_time": document.get("business_time") or document.get("timestamp")
        or document.get("generated_at") or document.get("created_at"),
        "ingested_at": document.get("ingested_at") or document.get("synced_at")
        or document.get("recorded_at"),
    }
