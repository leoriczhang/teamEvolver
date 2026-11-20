"""Formal Session console queries from PostgreSQL and immutable payloads."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

from ..projections import session_list_meta
from ..repository import SessionIngestionRepository
from ..skill_versions import session_skill_versions, snapshot_skill_ids
from ..stage_projection import STAGE_ORDER, stage_result
from . import _require_tenant_admin


def create_router(services):
    router = APIRouter(tags=["sessions"])
    tx = services.tx

    async def listing(request, limit, offset, search="", decision="", skill="", start="", end=""):
        _require_tenant_admin(request)
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(400, "invalid Session page")
        where = (
            "WHERE ($1='' OR session_id ILIKE '%'||$1||'%' OR meta::text ILIKE '%'||$1||'%') "
            "AND ($2='' OR decision=$2) AND ($3='' OR meta::text ILIKE '%'||$3||'%') "
            "AND ($4::text='' OR business_time>=$4::timestamptz) "
            "AND ($5::text='' OR business_time<=$5::timestamptz)"
        )
        params = (search, decision, skill, start, end)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await conn.fetch(
                f"SELECT session_id,user_id,source,decision,judge,meta,content_ref,content_hash,"
                f"business_time,ingested_at "
                f"FROM {tx.pool.schema}.sessions {where} ORDER BY ingested_at DESC,session_id DESC "
                "LIMIT $6 OFFSET $7", *params, limit, offset)
            total = await conn.fetchval(f"SELECT count(*) FROM {tx.pool.schema}.sessions {where}", *params)

        semaphore = asyncio.Semaphore(8)
        name_reader = services.extras.get("ma_session_skill_names")
        pending_names = {}

        async def names_for(session, meta):
            runtime = session.get("runtime") or {}
            connection = runtime.get("connection_id") or meta.get("ma_connection_id")
            ids = snapshot_skill_ids(session)
            if not name_reader or not connection or not ids:
                return {}
            key = (connection, tuple(ids))
            if key not in pending_names:
                pending_names[key] = asyncio.create_task(
                    name_reader(current_tenant_id(), connection, ids))
            return await pending_names[key]

        async def present(row):
            item = dict(row)
            for key in ("judge", "meta"):
                item[key] = json.loads(item[key]) if isinstance(item[key], str) else item[key]
            content_ref = item.get("content_ref", "")
            title = str(item["meta"].get("title") or "").strip()
            missing_title = not title and item["meta"].get("title_projection_empty") is not True
            if content_ref and (missing_title or item["meta"].get("skill_versions_revision") != 1):
                try:
                    async with semaphore:
                        session = await asyncio.to_thread(services.extras["session_payloads"].get_session, content_ref)
                        names = await names_for(session, item["meta"])
                        meta = session_list_meta(session, item["meta"], skill_names=names)
                        async with tx.tenant_tx(current_tenant_id()) as conn:
                            await SessionIngestionRepository(tx).cache_list_meta(conn, row=item, meta=meta)
                    item["meta"] = meta
                    title = str(meta.get("title") or "").strip()
                except (FileNotFoundError, KeyError, TypeError, ValueError):
                    pass
            item.pop("content_ref", None)
            item.pop("content_hash", None)
            item.update(title=title or item["session_id"],
                        user_alias=item["meta"].get("user_display_name") or item["user_id"],
                        status="archived", timestamp=item["business_time"],
                        num_turns=item["meta"].get("num_turns"),
                        used_skills=item["meta"].get("used_skills") or [],
                        available_skills=item["meta"].get("available_skills"),
                        used_skill_versions=item["meta"].get("used_skill_versions") or [],
                        available_skill_versions=item["meta"].get("available_skill_versions") or [])
            return item

        values = await asyncio.gather(*(present(row) for row in rows))
        return {"sessions": values, "total": total, "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total}

    @router.get("/api/sessions/page")
    async def page(request: Request, limit: int = 20, offset: int = 0, search: str = "", decision: str = "",
                   skill: str = "", start: str = "", end: str = ""):
        return await listing(request, limit, offset, search, decision, skill, start, end)

    @router.get("/api/sessions/analysis")
    async def analysis(request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await conn.fetch(f"SELECT decision,count(*) AS count FROM {tx.pool.schema}.sessions "
                                    "GROUP BY decision")
        return {"stats": {"total": sum(r["count"] for r in rows),
                          "decisions": {r["decision"]: r["count"] for r in rows}, "modes": {"model": sum(
                              r["count"] for r in rows)}}}

    @router.get("/api/sessions/export")
    async def export(request: Request, format: str = "json", ids: str = "", search: str = "", decision: str = ""):
        import csv
        import io

        from fastapi.responses import Response
        if format not in {"json", "csv"}:
            raise HTTPException(400, "format must be json or csv")
        page = await listing(request, 200, 0, search, decision)
        rows = [r for r in page["sessions"] if not ids or r["session_id"] in ids.split(",")]
        for row in rows:
            for key in ("timestamp", "business_time", "ingested_at"):
                if hasattr(row.get(key), "isoformat"):
                    row[key] = row[key].isoformat()
        if format == "json":
            content = json.dumps(rows, ensure_ascii=False)
        else:
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=["session_id", "user_id", "decision",
                                                       "business_time", "ingested_at"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue()
        return Response(content, media_type="application/json" if format == "json" else "text/csv",
                        headers={"X-Export-Count": str(len(rows))})

    @router.get("/api/sessions/{session_id}/content")
    async def content(session_id: str, request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await conn.fetchrow(f"SELECT content_ref,user_id,meta,judge,decision,business_time,ingested_at "
                                      f"FROM {tx.pool.schema}.sessions WHERE session_id=$1", session_id)
        if row is None:
            raise HTTPException(404, "Session not found")
        session = await asyncio.to_thread(services.extras["session_payloads"].get_session, row["content_ref"])
        meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else dict(row["meta"])
        turns = session.get("turns") if isinstance(session.get("turns"), list) else []
        runtime = session.get("runtime") if isinstance(session.get("runtime"), dict) else {}
        connection_id = runtime.get("connection_id") or meta.get("ma_connection_id")
        name_reader = services.extras.get("ma_session_skill_names")
        names = (await name_reader(current_tenant_id(), connection_id, snapshot_skill_ids(session))
                 if name_reader else {})
        return {
            **session, "user_id": row["user_id"],
            "skill_versions": session_skill_versions(session, names, meta=meta),
            "user_alias": meta.get("user_display_name") or row["user_id"],
            "meta": meta, "judge": json.loads(row["judge"]),
            "turns_available": bool(turns), "turns_source": "archive" if turns else "",
            "summary": {
                "title": meta.get("title") or session.get("title") or session_id,
                "user_alias": meta.get("user_display_name") or row["user_id"] or session.get("user_alias"),
                "status": "archived", "num_turns": len(turns),
            },
            "business_time": row["business_time"], "ingested_at": row["ingested_at"],
        }

    @router.get("/api/sessions/{session_id}/stages")
    async def stages(session_id: str, request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await conn.fetchrow(f"SELECT ingestion_id FROM {tx.pool.schema}.sessions WHERE session_id=$1",
                                      session_id)
            if row is None:
                raise HTTPException(404, "Session not found")
            results = await conn.fetch(f"SELECT stage,status,created_at,updated_at,error_code "
                                       f"FROM {tx.pool.schema}.session_stage_results WHERE ingestion_id=$1",
                                       row["ingestion_id"])
        ranks = {name: index for index, name in enumerate(STAGE_ORDER)}
        return {"stages": sorted((dict(r) for r in results), key=lambda r: (
            ranks.get(r["stage"], len(ranks)), r["stage"]))}

    @router.get("/api/sessions/{session_id}/stages/{stage}")
    async def stage_detail(session_id: str, stage: str, request: Request):
        _require_tenant_admin(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await conn.fetchrow(f"SELECT ingestion_id,user_id FROM {tx.pool.schema}.sessions WHERE session_id=$1",
                                      session_id)
            if row is None:
                raise HTTPException(404, "Session not found")
            result = await conn.fetchrow(
                f"SELECT stage,status,detail,error_code,created_at,updated_at,result_ref "
                f"FROM {tx.pool.schema}.session_stage_results WHERE ingestion_id=$1 AND stage=$2",
                row["ingestion_id"], stage,
            )
        if result is None:
            raise HTTPException(404, "Session stage not found")
        item = dict(result)
        ref = item.pop("result_ref")
        item["detail"] = json.loads(item["detail"]) if isinstance(item["detail"], str) else item["detail"]
        item.update(result_available=False, result=None)
        if ref:
            try:
                session = await asyncio.to_thread(services.extras["session_payloads"].get_session, ref)
            except (FileNotFoundError, KeyError):
                item["result_unavailable_reason"] = "阶段产物暂不可读取"
            else:
                item.update(result_available=True, result=stage_result(stage, {**session, "user_id": row["user_id"]}))
        else:
            item["result_unavailable_reason"] = "该阶段尚未保存产物"
        return item

    return router
