"""PG commands, branch checkpoints, evidence and consumption owned by Skills."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from team_skills.library.repository import SkillLibraryError, SkillMutationConflict


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unpack(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def lock_key(conn, tenant_id: str, key: str) -> None:
    """Transaction lock also covers absence of a row; never uses a process lock."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{tenant_id}:{key}")


class EvolutionRepository:
    def __init__(self, tx) -> None:
        self.tx = tx
        self.schema = tx.pool.schema

    async def command(self, conn, command_id: str) -> dict | None:
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.skill_commands WHERE command_id=$1", command_id,
        )
        if row is None:
            return None
        item = dict(row)
        item["payload"] = unpack(item["payload"])
        item["result"] = unpack(item["result"])
        return item

    async def accept(self, conn, *, tenant_id: str, command_id: str, kind: str,
                     request: dict, payload: dict) -> tuple[dict, bool]:
        await lock_key(conn, tenant_id, f"command:{command_id}")
        request_hash = digest(request)
        existing = await self.command(conn, command_id)
        if existing:
            if existing["request_hash"] != request_hash or existing["kind"] != kind:
                raise SkillMutationConflict("Idempotency-Key was used for a different command")
            return existing, False
        raw = canonical(payload)
        if len(raw.encode()) > 8 * 1024 * 1024:
            raise SkillLibraryError("skill command exceeds 8 MiB")
        await conn.execute(
            f"INSERT INTO {self.schema}.skill_commands "
            "(tenant_id,command_id,kind,request_hash,payload_hash,payload) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
            tenant_id, command_id, kind, request_hash, digest(payload), raw,
        )
        return await self.command(conn, command_id), True

    async def finish(self, conn, command_id: str, result: dict) -> None:
        await conn.execute(
            f"UPDATE {self.schema}.skill_commands SET result=$2::jsonb, finished_at=now() "
            "WHERE command_id=$1 AND result IS NULL", command_id, canonical(result),
        )

    async def validation_cost_runs(self, conn, candidate_id, *, excluding):
        rows = await conn.fetch(
            f"SELECT command_id,payload,finished_at FROM {self.schema}.skill_commands "
            "WHERE kind='skill.validate' AND payload->>'candidate_id'=$1 AND command_id<>$2 "
            "ORDER BY created_at LIMIT 101", candidate_id, excluding,
        )
        return [{**dict(row), "payload": unpack(row["payload"])} for row in rows]

    async def branch(self, conn, command_id: str, key: str) -> dict | None:
        row = await conn.fetchrow(
            f"SELECT result FROM {self.schema}.skill_evolution_branches "
            "WHERE command_id=$1 AND branch_key=$2", command_id, key,
        )
        return unpack(row["result"]) if row else None

    async def save_branch(self, conn, *, tenant_id: str, command_id: str, key: str,
                          session_ids: list[str], result: dict) -> None:
        await conn.execute(
            f"INSERT INTO {self.schema}.skill_evolution_branches "
            "(tenant_id,command_id,branch_key,session_ids,result) VALUES ($1,$2,$3,$4,$5::jsonb)",
            tenant_id, command_id, key, session_ids, canonical(result),
        )

    async def consumed(self, conn, sessions: list[dict], *, scope: str = "") -> set[tuple[str, str]]:
        rows = await conn.fetch(
            f"SELECT s.session_id,s.content_hash FROM {self.schema}.skill_evolution_sessions s "
            "WHERE s.session_id=ANY($1::text[]) AND (s.consumption_scope=$2 OR ("
            "$2<>'' AND s.consumption_scope='' AND EXISTS ("
            f"SELECT 1 FROM {self.schema}.skill_commands c "
            "WHERE c.tenant_id=s.tenant_id AND c.command_id=s.command_id AND c.kind='skill.evolve' "
            "AND c.result IS NOT NULL AND c.finished_at IS NOT NULL "
            "AND 'execution:' || (c.payload->>'execution_profile_id')=$2 "
            "AND c.payload->'allowed_modules' ? 'prompt' "
            "AND c.payload #> '{execution_baseline,profile,affected_skill_ids}'='[]'::jsonb "
            "AND 'execution:' || (c.payload #>> '{execution_baseline,profile,profile_id}')=$2)))",
            [s["session_id"] for s in sessions], scope,
        )
        return {(r["session_id"], r["content_hash"]) for r in rows}

    async def consume(self, conn, *, tenant_id: str, command_id: str, sessions: list[dict], scope: str = "") -> None:
        await conn.executemany(
            f"INSERT INTO {self.schema}.skill_evolution_sessions "
            "(tenant_id,session_id,content_hash,command_id,consumption_scope) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT DO NOTHING",
            [(tenant_id, s["session_id"], s["content_hash"], command_id, scope) for s in sessions],
        )

    async def evidence(self, conn, name: str, limit: int = 24, *, include_source=False) -> list[dict]:
        rows = await conn.fetch(
            "SELECT projection,content_hash FROM (SELECT DISTINCT ON "
            "(COALESCE(NULLIF(projection #>> '{provenance,source_session_key}',''),session_id)) "
            f"projection,content_hash,captured_at,session_id FROM {self.schema}.skill_evidence WHERE skill_name=$1 "
            "ORDER BY COALESCE(NULLIF(projection #>> '{provenance,source_session_key}',''),session_id),"
            "captured_at DESC,session_id DESC) sources "
            "ORDER BY captured_at DESC,session_id LIMIT $2", name, min(200, max(1, limit)),
        )
        result = []
        for row in rows:
            item = dict(unpack(row["projection"]))
            source = item.pop("prompt_source", {})
            if include_source:
                item.update(source)
                item["content_hash"] = row["content_hash"]
            result.append(item)
        return result

    async def record_evidence(self, conn, *, tenant_id: str, name: str, sessions: list[dict]) -> None:
        projections = []
        for s in sessions:
            projection = {
                "session_id": s["session_id"], "user_alias": s.get("user_alias", ""),
                "_summary": str(s.get("_summary") or "")[:16000],
                "_judge_scores": s.get("_judge_scores") or {}, "_evidence_window": "historical",
                "runtime_context": s.get("runtime_context") or {},
                "source": s.get("source", ""),
            }
            if "provenance" in s:
                projection["provenance"] = s["provenance"]
            # Any registered Skill may later be selected together with its
            # business Prompt. Keep original material for that path as well;
            # analytical summaries are indexes, not replacement source facts.
            original = {field: s[field] for field in (
                "turns", "_evidence_turns", "runtime", "analysis_context", "messages", "system_prompt",
                "prompt_provenance", "context_usage",
            ) if field in s}
            if name.startswith("execution:"):
                projection.update(original)
            else:
                projection["prompt_source"] = original
            if (s.get("metadata") or {}).get("split_from_session"):
                projection["metadata"] = {"split_from_session": s["metadata"]["split_from_session"]}
            elif (s.get("meta") or {}).get("split_from_session"):
                projection["metadata"] = {"split_from_session": s["meta"]["split_from_session"]}
            projections.append((tenant_id, name, s["session_id"], s["content_hash"], canonical(projection)))
        await conn.executemany(
            f"INSERT INTO {self.schema}.skill_evidence "
            "(tenant_id,skill_name,session_id,content_hash,projection) VALUES ($1,$2,$3,$4,$5::jsonb) "
            "ON CONFLICT (tenant_id,skill_name,session_id) DO NOTHING", projections,
        )

    async def history(self, conn, *, session_id: str = "", limit: int = 20) -> list[dict]:
        rows = await conn.fetch(
            f"SELECT c.command_id,c.created_at,c.finished_at,c.result,"
            "c.payload->'allowed_modules' AS allowed_modules,c.payload->'config_revision' AS config_revision "
            f"FROM {self.schema}.skill_commands c "
            "WHERE c.kind='skill.evolve' AND ($1='' OR EXISTS ("
            f"SELECT 1 FROM {self.schema}.skill_evolution_branches b "
            "WHERE b.tenant_id=c.tenant_id AND b.command_id=c.command_id AND $1=ANY(b.session_ids))) "
            "ORDER BY c.created_at DESC LIMIT $2", session_id, min(200, max(1, limit)),
        )
        return [{**dict(r), "result": unpack(r["result"]),
                 "allowed_modules": unpack(r["allowed_modules"]),
                 "config_revision": unpack(r["config_revision"])} for r in rows]
