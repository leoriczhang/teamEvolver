"""Read-only verification contract for ingestion consumers of PG Context."""
from __future__ import annotations

from .repository import ContextRepository, stable_hash


async def verify_usage(services, principal, turns):
    repo = ContextRepository(services.tx)
    verified = []
    async with services.tx.tenant_tx(principal.tenant_id) as conn:
        for turn in turns:
            usage = turn.get("context_usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("context_usage must be an object")
            sid = str(usage.get("context_snapshot_id") or "")
            snapshot = await repo.load_snapshot(conn, principal, sid) if sid else None
            if sid and snapshot is None:
                raise ValueError("invalid context snapshot")
            result = {"context_snapshot_id": sid, "memory_refs": [], "skill_refs": [],
                      "feedback": dict(usage.get("feedback") or {}), "verified": True}
            for field, kind in (("memory_refs", "memory"), ("skill_refs", "skill")):
                refs = usage.get(field) or []
                if not isinstance(refs, list) or len(refs) > 200:
                    raise ValueError("invalid context usage refs")
                for item in refs:
                    if not isinstance(item, dict):
                        raise ValueError("context usage ref must be an object")
                    rid = str(item.get("context_ref") or "")
                    ref = await repo.ref(conn, principal, rid)
                    if ref is None or ref["kind"] != kind:
                        raise ValueError("invalid or expired context reference")
                    if snapshot and rid not in {i["context_ref"] for i in snapshot["items"]}:
                        raise ValueError("context reference does not belong to snapshot")
                    operation = str(item.get("operation") or "retrieved")
                    if operation not in {"retrieved", "injected", "read", "selected"}:
                        raise ValueError("unsupported context usage operation")
                    entry = {"context_ref": rid, "operation": operation, "scope": ref["scope"],
                             "version": ref["version"], "uri_hash": stable_hash(ref["uri"])}
                    if kind == "skill":
                        prefix = "personal" if ref["scope"] == "personal_skills" else "team"
                        entry["qualified_skill_id"] = prefix + ":" + ref["uri"].rsplit("/", 1)[-1]
                    result[field].append(entry)
            verified.append({**turn, "context_usage": result})
    return verified
