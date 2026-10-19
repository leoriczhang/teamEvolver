"""Memory Replay producer using only the public Replay contract."""
from __future__ import annotations

import hashlib

from team_replay.contracts import ReplaySpec, ReplayTreatment
from team_replay.datasets.schema import DATASET_SCHEMA, normalize_document


def specification(rid, subject, raw, before, after, uri):
    query = str(raw.get("query") or "").strip()
    checks = raw.get("checklist") or []
    if isinstance(checks, str):
        checks = [c for c in checks.splitlines() if c.strip()]
    if not query or not checks:
        raise ValueError("query and checklist required")
    checks = [c if isinstance(c, dict) else {"id": f"c{i}", "kind": "output", "text": c}
              for i, c in enumerate(checks)]
    doc = normalize_document({"schema_version": DATASET_SCHEMA, "dataset_id": "memory-replay", "name": subject,
                              "skills": [], "cases": [{"case_id": "memory", "query": query, "checks": checks,
                                                       "materials": [], "skill_ids": []}]})
    def branch(content):
        sha = hashlib.sha256(content.encode()).hexdigest()
        return ReplayTreatment("memory", sha, {"uri": uri, "revision": sha, "content": content}, content_hash=sha)
    runtime = raw.get("runtime") or {"type": "configured"}
    return ReplaySpec(rid, "memory", subject, DATASET_SCHEMA, tuple(doc["cases"]),
                      branch(before), branch(after), runtime=runtime)
