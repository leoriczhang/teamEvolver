"""Durable, tenant-scoped pull membership; source snapshots can belong to many runs."""
from __future__ import annotations

import json


class PullRunRepository:
    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def start(self, conn, *, tenant_id, run_id):
        await conn.execute(
            f"-- te:op=pull_run_start\nINSERT INTO {self.schema}.session_pull_runs(tenant_id,run_id) "
            "VALUES($1,$2) ON CONFLICT DO NOTHING", tenant_id, run_id,
        )

    async def exists(self, conn, run_id):
        return bool(await conn.fetchval(
            f"-- te:op=pull_run_exists\nSELECT 1 FROM {self.schema}.session_pull_runs WHERE run_id=$1", run_id,
        ))

    async def member(self, conn, run_id, external_id):
        row = await conn.fetchrow(
            f"-- te:op=pull_member_get\nSELECT * FROM {self.schema}.session_pull_members "
            "WHERE run_id=$1 AND external_id=$2", run_id, external_id,
        )
        return dict(row) if row else None

    async def record(self, conn, *, tenant_id, run_id, external_id, acceptance, ingestion_id=None,
                     provenance=None, error_code=""):
        p = provenance or {}
        # Once accepted, this run keeps that exact snapshot even if a retry
        # fetches a subsequently edited upstream Session. Errors/empty results
        # may recover on a retry; accepted membership cannot be overwritten.
        await conn.execute(
            f"-- te:op=pull_member_record\nINSERT INTO {self.schema}.session_pull_members AS m "
            "(tenant_id,run_id,external_id,acceptance,ingestion_id,source_session_key,snapshot_revision,error_code) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(tenant_id,run_id,external_id) DO UPDATE SET "
            "acceptance=EXCLUDED.acceptance,ingestion_id=EXCLUDED.ingestion_id,"
            "source_session_key=EXCLUDED.source_session_key,snapshot_revision=EXCLUDED.snapshot_revision,"
            "error_code=EXCLUDED.error_code,updated_at=now() WHERE m.ingestion_id IS NULL",
            tenant_id, run_id, external_id, acceptance, ingestion_id,
            p.get("source_session_key", ""), p.get("snapshot_revision", ""), error_code,
        )

    async def counts(self, conn, run_id):
        rows = await conn.fetch(
            f"-- te:op=pull_member_counts\nSELECT acceptance,count(*) AS n "
            f"FROM {self.schema}.session_pull_members WHERE run_id=$1 GROUP BY acceptance", run_id,
        )
        counts = dict.fromkeys(("fetched", "enqueued", "duplicate", "empty", "error"), 0)
        for row in rows:
            counts[row["acceptance"]] = row["n"]
            counts["fetched"] += row["n"]
        return counts

    async def page(self, conn, run_id, *, after="", limit=200):
        return [dict(row) for row in await conn.fetch(
            f"SELECT external_id,acceptance,ingestion_id,source_session_key,snapshot_revision,error_code "
            f"FROM {self.schema}.session_pull_members WHERE run_id=$1 AND external_id>$2 "
            "ORDER BY external_id LIMIT $3", run_id, after, limit,
        )]

    async def nodes(self, conn, roots):
        """Follow persisted split edges, including missing fan-out children.

        Only domain tables are read here. Platform task state is composed by
        the progress read model via the runtime repository's public query.
        Bound pathological depth/size and report incompleteness to the caller.
        """
        rows = await conn.fetch(
            f"WITH RECURSIVE tree(root,node,path) AS ("
            "SELECT r,r,ARRAY[r] FROM unnest($1::text[]) r UNION ALL "
            f"SELECT t.root,c.child,t.path||c.child FROM tree t JOIN {self.schema}.session_stage_results s "
            "ON s.ingestion_id=t.node AND s.stage='split' CROSS JOIN LATERAL "
            "jsonb_array_elements_text(CASE WHEN jsonb_typeof(s.detail->'children')='array' "
            "THEN s.detail->'children' ELSE '[]'::jsonb END) c(child) "
            "WHERE NOT c.child=ANY(t.path) AND cardinality(t.path)<32) "
            "SELECT t.root,t.node,cardinality(t.path) AS depth,i.status AS ingestion_status,"
            "COALESCE(s.detail->'children','[]'::jsonb) AS children,"
            "s.status AS split_status,a.status AS archive_status,"
            f"COALESCE((SELECT jsonb_agg(f.session_id ORDER BY f.session_id) FROM {self.schema}.sessions f "
            "WHERE f.ingestion_id=t.node),'[]'::jsonb) AS session_ids "
            f"FROM tree t LEFT JOIN {self.schema}.ingestions i ON i.ingestion_id=t.node "
            f"LEFT JOIN {self.schema}.session_stage_results s ON s.ingestion_id=t.node AND s.stage='split' "
            f"LEFT JOIN {self.schema}.session_stage_results a ON a.ingestion_id=t.node AND a.stage='archive' "
            "ORDER BY t.root,t.node LIMIT 10001", list(dict.fromkeys(roots)),
        )
        values = []
        for row in rows[:10000]:
            item = dict(row)
            for key in ("children", "session_ids"):
                if isinstance(item[key], str):
                    item[key] = json.loads(item[key])
            values.append(item)
        return values, len(rows) > 10000 or any(v["children"] and v["depth"] >= 32 for v in values)
