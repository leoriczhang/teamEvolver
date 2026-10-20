"""Mandatory model analysis and immutable intermediate Session stages."""
from __future__ import annotations

import asyncio
import copy


class SessionStages:
    def __init__(self, payloads, split, analyze, trace_analyze):
        self.payloads = payloads
        self.split_call = split
        self.analyze_call = analyze
        self.trace_call = trace_analyze

    async def previous(self, payload, stage):
        from teamEvolver.tenants.registry import current_tenant_id
        async def read(conn):
            return await conn.fetchval(
                f"SELECT result_ref FROM {self.payloads.tx.pool.schema}.session_stage_results "
                "WHERE ingestion_id=$1 AND stage=$2 AND status='succeeded'",
                payload.ingestion_id, stage,
            )
        async with self.payloads.tx.tenant_tx(current_tenant_id()) as conn:
            ref = await read(conn)
        if not ref:
            raise ValueError(f"mandatory {stage} output is missing")
        return await asyncio.to_thread(self.payloads.get_session, ref)

    async def normalize(self, session, *, payload):
        if not isinstance(session.get("turns"), list) or not session["turns"]:
            raise ValueError("Session requires turns")
        return copy.deepcopy(session)

    async def split(self, session, *, payload):
        session.clear()
        session.update(await self.previous(payload, "normalize"))
        return await self.split_call(session)

    async def analyze(self, session, *, payload):
        plan = await self.previous(payload, "split")
        segments = plan.get("segments") if isinstance(plan, dict) else None
        if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
            raise ValueError("split stage must provide exactly one Session for analysis")
        session.clear()
        session.update(segments[0])
        return await self.analyze_call(session)

    async def trace_analyze(self, session, *, payload):
        session["trace_analysis"] = await self.trace_call(session)

    async def archive(self, session, *, payload):
        session = await self.previous(payload, "trace_analyze")
        decision = session["value_judge"]["decision"]
        return {
            "decision": "valuable" if decision == "valuable" else "skipped",
            "session": session, "judge": session["_judge_scores"],
            "filter_audit": session["value_judge"], "meta": session.get("meta", {}),
            "business_time": session.get("business_time") or session.get("timestamp"),
        }
