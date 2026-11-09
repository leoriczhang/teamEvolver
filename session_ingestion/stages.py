"""Mandatory model analysis and immutable intermediate Session stages."""
from __future__ import annotations

import asyncio
import copy

from .source_repository import SourceRevisionRepository
from .source_revisions import POLICY, digest, preserve_requests, trace_id


class SessionStages:
    def __init__(self, payloads, split, analyze, trace_analyze, *, analysis_identity=None):
        self.payloads = payloads
        self.split_call = split
        self.analyze_call = analyze
        self.trace_call = trace_analyze
        self.analysis_identity = analysis_identity

    async def _dependencies(self):
        if self.analysis_identity is None:
            return None
        return digest({"policy": POLICY, "dependencies": await self.analysis_identity()})

    async def analysis_checkpoint_valid(self, session):
        dependencies = await self._dependencies()
        return dependencies is not None and session.get("_analysis_dependencies") == dependencies

    async def _source_context(self, session):
        provenance = session.get("provenance") or {}
        ref = provenance.get("full_snapshot_ref")
        if ref:
            original = await asyncio.to_thread(self.payloads.get_session, ref)
            targets = {r["trace_id"] for r in provenance.get("target_trace_refs") or []}
            context = [r for r in (original.get("provenance") or {}).get("trace_revisions") or []
                       if r["trace_id"] not in targets]
            session["analysis_context"] = {
                "turns": copy.deepcopy(original.get("turns") or []),
                "system_prompt": copy.deepcopy(original.get("system_prompt")),
                "full_snapshot_ref": ref, "context_trace_refs": context,
                "context_only": True,
            }

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
        return preserve_requests(copy.deepcopy(session), [])

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
        await self._source_context(session)
        dependencies = await self._dependencies()
        session["_analysis_dependencies"] = dependencies
        provenance = session.get("provenance") or {}
        provenance["analysis_revision"] = digest({
            "dependencies": dependencies, "snapshot_revision": provenance.get("snapshot_revision"),
        })
        session["provenance"] = provenance
        return await self.analyze_call(session)

    async def trace_analyze(self, session, *, payload):
        provenance = session.get("provenance") or {}
        if not provenance.get("identity_verified") or not provenance.get("trace_identity_verified"):
            session["trace_analysis"] = await self.trace_call(session)
            return
        targets = provenance.get("target_trace_refs") or []
        target_ids = {ref["trace_id"] for ref in targets}
        work = copy.deepcopy(session)
        # The original snapshot remains complete. Only analysis targets change.
        work["analysis_context"] = copy.deepcopy(session.get("analysis_context") or {
            "turns": session.get("turns") or [], "context_only": True,
            "context_trace_refs": provenance.get("context_trace_refs") or [],
        })
        work["turns"] = [turn for turn in work.get("turns") or [] if trace_id(turn) in target_ids]
        work["provenance"]["context_only"] = False
        identity = {"source": provenance["source_session_key"], "targets": targets,
                    "dependencies": session.get("_analysis_dependencies"),
                    "actual_context": work.get("analysis_context"), "summary": session.get("_summary")}
        key = digest(identity)
        sources = SourceRevisionRepository(self.payloads.tx)
        cached = None
        if self.analysis_identity is not None:
            async with self.payloads.tx.tenant_tx(payload.tenant_id) as conn:
                cached = await sources.cached(conn, key)
        if cached:
            stored = await asyncio.to_thread(self.payloads.get_session, cached)
            results = stored["trace_analysis"]
        else:
            results = await self.trace_call(work) if targets else []
            if not isinstance(results, list) or len(results) != len(target_ids) or {
                str(result.get("trace_id") or "") for result in results
            } != target_ids:
                raise ValueError("Trace analysis must cover each target Trace exactly once")
            if self.analysis_identity is not None:
                ref, _ = await asyncio.to_thread(
                    self.payloads.put_session, payload.ingestion_id, {"trace_analysis": results},
                    stage="trace-analysis-" + key,
                )
                async with self.payloads.tx.tenant_tx(payload.tenant_id) as conn:
                    await sources.cache(conn, tenant_id=payload.tenant_id, key=key, result_ref=ref)
        positions = {trace_id(t): i for i, t in enumerate(session.get("turns") or [], 1)}
        session["trace_analysis"] = [
            {**result, "turn_num": positions.get(str(result.get("trace_id")), result.get("turn_num"))}
            for result in results
        ]
        session["trace_analysis_coverage"] = {
            "target_count": len(targets), "context_count": len(provenance.get("context_trace_refs") or []),
            "complete_for_targets": True, "context_only_is_not_reanalyzed": True,
        }

    async def archive(self, session, *, payload):
        session = await self.previous(payload, "trace_analyze")
        decision = session["value_judge"]["decision"]
        return {
            "decision": "valuable" if decision == "valuable" else "skipped",
            "session": session, "judge": session["_judge_scores"],
            "filter_audit": session["value_judge"], "meta": session.get("meta", {}),
            "business_time": session.get("business_time") or session.get("timestamp"),
        }
