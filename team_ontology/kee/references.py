"""Resolve documentary references in the existing tenant object store; never copy traces."""

import hashlib
import json
from urllib.parse import unquote

from fastapi import HTTPException


async def validate_references(db, tenant, pack):
    bindings = [binding for method in pack.methods for binding in method.capabilities]
    cases = [case for method in pack.methods for case in method.cases]
    if not bindings and not cases:
        return
    async with db.transaction(tenant) as conn:
        if bindings:
            rows = await conn.fetch(
                f"SELECT content FROM {db.table} WHERE tenant_id=$1 AND "
                "(key='evolve_skill_registry.json' OR key LIKE '%/evolve_skill_registry.json') LIMIT 100",
                tenant,
            )
            versions = set()
            for row in rows:
                if len(row["content"]) > 1024 * 1024:
                    raise HTTPException(413, "SKILL_REGISTRY_SIZE_LIMIT")
                registry = json.loads(bytes(row["content"]))
                for value in registry.values():
                    if isinstance(value, dict) and value.get("skill_id"):
                        versions.add((value["skill_id"], str(value.get("version"))))
                        for old in value.get("history", []):
                            if isinstance(old, dict):
                                versions.add((value["skill_id"], str(old.get("version"))))
            if any((binding.skill_id, binding.version) not in versions for binding in bindings):
                raise HTTPException(422, "CAPABILITY_SKILL_VERSION_NOT_FOUND")
        for case in cases:
            key = unquote(case.reference)
            allowed = {
                "Session": ("sessions/", "session_index/"),
                "Evidence": ("skill_evidence/", "evidence/"),
                "receipt": ("kee/r6/releases/", "candidates/"),
            }[case.kind]
            if not key.startswith(allowed) or any(part in {"..", "."} for part in key.split("/")):
                raise HTTPException(422, "CASE_REFERENCE_OBJECT_KEY_REQUIRED")
            # Bounded read solely for hashing; never persist or return the existing Session body.
            row = await conn.fetchrow(
                f"SELECT octet_length(content) AS size,content FROM {db.table} "
                "WHERE tenant_id=$1 AND key=$2 AND octet_length(content)<=8388608",
                tenant,
                key,
            )
            if not row:
                raise HTTPException(422, "CASE_REFERENCE_NOT_FOUND_OR_OVERSIZED")
            checksum = hashlib.sha256(bytes(row["content"])).hexdigest()
            if case.digest and case.digest != checksum:
                raise HTTPException(409, "CASE_REFERENCE_DIGEST_CHANGED")
            if case.outcome != "unknown" and not case.digest:
                raise HTTPException(422, "CASE_OUTCOME_REQUIRES_DIGEST")
