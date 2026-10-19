"""Select archived Sessions through the public query and payload contracts."""
from __future__ import annotations

import asyncio

from session_ingestion.queries import SessionQueries
from session_ingestion.schemas import session_payload_hash

from .repository import DatasetError, DatasetNotFound
from .session_snapshot import snapshot_case


async def select_cases(conn, services, selection):
    if set(selection) - {"session_ids", "filters"}:
        raise DatasetError("selection accepts session_ids or filters")
    ids = selection.get("session_ids")
    filters = selection.get("filters")
    if (ids is None) == (filters is None):
        raise DatasetError("choose session_ids or filters")
    queries = SessionQueries(services.tx)
    if ids is not None:
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(not isinstance(i, str) for i in ids):
            raise DatasetError("select 1..500 Session IDs")
        rows = []
        for sid in dict.fromkeys(ids):
            row = await queries.get_session(conn, session_id=sid)
            if row is None:
                raise DatasetNotFound("selected Session not found")
            rows.append(row)
    else:
        if not isinstance(filters, dict) or set(filters) - {"search", "decision", "start", "end", "skill"}:
            raise DatasetError("unsupported Session filters")
        rows = []
        after = None
        while True:
            page = await queries.dataset_page(conn, after=after, filters=filters, limit=200)
            rows.extend(page)
            if len(rows) > 500:
                raise DatasetError("selection exceeds 500 Sessions; narrow filters")
            if len(page) < 200:
                break
            after = (page[-1]["ingested_at"], page[-1]["session_id"])
    payloads = services.extras.get("session_payloads")
    if payloads is None:
        raise DatasetError("Session payload storage is not configured")
    cases = []
    for row in rows:
        session = await asyncio.to_thread(payloads.get_session, row["content_ref"])
        if session_payload_hash(session) != row["content_hash"]:
            raise DatasetError("Session payload hash mismatch")
        case = snapshot_case(session, {
            **row, "timestamp": row["business_time"].isoformat() if row.get("business_time") else "",
            "ingested_at": row["ingested_at"].isoformat(), "meta": row.get("meta") or {},
        })
        case["provenance"]["source_runtime"] = session.get("runtime") or {"type": row.get("source", "")}
        cases.append(case)
    if not cases:
        raise DatasetError("selection has no Sessions")
    return cases
