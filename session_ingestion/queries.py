"""Read-only bounded, tenant-scoped Session queries for API and domain consumers."""

from __future__ import annotations

import json
from typing import Any

from teamEvolver.database.transaction import TransactionManager

# Hard upper bound on a single page so a caller cannot ask for an unbounded scan.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def _loads(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _clamp_limit(limit: int | None) -> int:
    if not limit or limit <= 0:
        return DEFAULT_PAGE_SIZE
    return min(int(limit), MAX_PAGE_SIZE)


class SessionQueries:
    """Bounded read models over ``sessions`` / ``ingestions``."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    async def list_sessions(
        self, conn: Any, *, limit: int | None = None, before: Any = None
    ) -> list[dict[str, Any]]:
        """Keyset page of formal Sessions, newest business_time first.

        ``before`` is the exclusive upper bound on ``business_time`` for the next
        page (keyset pagination — no OFFSET scan). Returns both the business
        occurrence time and the ingest time on every row.
        """
        page = _clamp_limit(limit)
        rows = await conn.fetch(
            f"-- te:op=session_list\n"
            f"SELECT session_id, source, external_id, user_id, decision, judge, "
            "  business_time, ingested_at "
            f"FROM {self._schema}.sessions "
            "WHERE ($1::timestamptz IS NULL OR business_time < $1) "
            "ORDER BY business_time DESC NULLS LAST, session_id DESC "
            "LIMIT $2",
            before,
            page,
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["judge"] = _loads(item.get("judge"))
            out.append(item)
        return out

    async def get_session(self, conn: Any, *, session_id: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"-- te:op=session_get\n"
            f"SELECT session_id, ingestion_id, source, external_id, user_id, account_id, "
            "  decision, content_ref, content_hash, judge, filter_audit, consumption, "
            "  meta, business_time, ingested_at "
            f"FROM {self._schema}.sessions WHERE session_id = $1",
            session_id,
        )
        if row is None:
            return None
        item = dict(row)
        for key in ("judge", "filter_audit", "consumption", "meta"):
            item[key] = _loads(item.get(key))
        return item

    async def evolution_page(self, conn: Any, *, session_ids: list[str] | None = None,
                             after: tuple | None = None, limit: int = 100) -> list[dict]:
        """Public read contract: valuable, fully archived inputs in stable order."""
        rows = await conn.fetch(
            f"SELECT session_id,user_id,content_ref,content_hash,business_time,ingested_at "
            f"FROM {self._schema}.sessions WHERE decision='valuable' "
            "AND ($1::text[] IS NULL OR session_id=ANY($1)) "
            "AND ($2::timestamptz IS NULL OR (ingested_at,session_id)>($2,$3)) "
            "ORDER BY ingested_at,session_id LIMIT $4",
            session_ids, after[0] if after else None, after[1] if after else "",
            _clamp_limit(limit),
        )
        return [dict(r) for r in rows]

    async def experience_page(self, conn, *, until, after=None, since=None, limit=100):
        """Public read contract for model-authored lessons in archived Sessions."""
        rows = await conn.fetch(
            f"SELECT session_id,user_id,judge,business_time,ingested_at FROM {self._schema}.sessions "
            "WHERE ingested_at <= $1 AND ($2::timestamptz IS NULL OR ingested_at >= $2) "
            "AND ($3::timestamptz IS NULL OR (ingested_at,session_id)>($3,$4)) "
            "ORDER BY ingested_at,session_id LIMIT $5",
            until, since, after[0] if after else None, after[1] if after else "", _clamp_limit(limit),
        )
        return [{**dict(r), "judge": _loads(r["judge"])} for r in rows]

    async def dataset_page(self, conn, *, filters, after=None, limit=200):
        """Bounded keyset query for Dataset selection, returning immutable refs."""
        rows = await conn.fetch(
            f"SELECT session_id,source,user_id,content_ref,content_hash,meta,judge,business_time,ingested_at "
            f"FROM {self._schema}.sessions "
            "WHERE ($1='' OR decision=$1) AND ($2='' OR session_id ILIKE '%'||$2||'%' "
            "OR meta::text ILIKE '%'||$2||'%') "
            "AND ($3::text='' OR business_time >= $3::timestamptz) "
            "AND ($4::text='' OR business_time <= $4::timestamptz) "
            "AND ($5='' OR meta::text ILIKE '%'||$5||'%') "
            "AND ($6::timestamptz IS NULL OR (ingested_at,session_id)>($6,$7)) "
            "ORDER BY ingested_at,session_id LIMIT $8",
            filters.get("decision", ""), filters.get("search", ""), filters.get("start", ""),
            filters.get("end", ""), filters.get("skill", ""), after[0] if after else None,
            after[1] if after else "", _clamp_limit(limit),
        )
        return [{**dict(row), "meta": _loads(row["meta"]), "judge": _loads(row["judge"])} for row in rows]


__all__ = ["SessionQueries", "MAX_PAGE_SIZE", "DEFAULT_PAGE_SIZE"]
