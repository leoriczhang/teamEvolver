"""Formal Session console queries from PostgreSQL and immutable payloads."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

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
                f"SELECT session_id,user_id,source,decision,judge,meta,business_time,ingested_at "
                f"FROM {tx.pool.schema}.sessions {where} ORDER BY ingested_at DESC,session_id DESC "
                "LIMIT $6 OFFSET $7", *params, limit, offset)
            total = await conn.fetchval(f"SELECT count(*) FROM {tx.pool.schema}.sessions {where}", *params)
        values = []
        for row in rows:
            item = dict(row)
            for key in ("judge", "meta"):
                item[key] = json.loads(item[key]) if isinstance(item[key], str) else item[key]
            item.update(title=item["meta"].get("title", item["session_id"]), user_alias=item["user_id"],
                        status="archived", timestamp=item["business_time"],
                        num_turns=item["meta"].get("num_turns"),
                        used_skills=item["meta"].get("used_skills") or [])
            values.append(item)
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
        return {
            **session, "meta": meta, "judge": json.loads(row["judge"]),
            "turns_available": bool(turns), "turns_source": "archive" if turns else "",
            "summary": {
                "title": meta.get("title") or session.get("title") or session_id,
                "user_alias": row["user_id"] or meta.get("user_id") or session.get("user_alias"),
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
        return {"stages": [dict(r) for r in results]}

    return router
