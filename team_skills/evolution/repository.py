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

    async def consumed(self, conn, sessions: list[dict]) -> set[tuple[str, str]]:
        rows = await conn.fetch(
            f"SELECT session_id,content_hash FROM {self.schema}.skill_evolution_sessions "
            "WHERE session_id=ANY($1::text[])", [s["session_id"] for s in sessions],
        )
        return {(r["session_id"], r["content_hash"]) for r in rows}

    async def consume(self, conn, *, tenant_id: str, command_id: str, sessions: list[dict]) -> None:
        await conn.executemany(
            f"INSERT INTO {self.schema}.skill_evolution_sessions "
            "(tenant_id,session_id,content_hash,command_id) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING",
            [(tenant_id, s["session_id"], s["content_hash"], command_id) for s in sessions],
        )

    async def evidence(self, conn, name: str, limit: int = 24) -> list[dict]:
        rows = await conn.fetch(
            f"SELECT projection FROM {self.schema}.skill_evidence WHERE skill_name=$1 "
            "ORDER BY captured_at DESC,session_id LIMIT $2", name, min(200, max(1, limit)),
        )
        return [unpack(r["projection"]) for r in rows]

    async def record_evidence(self, conn, *, tenant_id: str, name: str, sessions: list[dict]) -> None:
        projections = []
        for s in sessions:
            projection = {
                "session_id": s["session_id"], "user_alias": s.get("user_alias", ""),
                "_summary": str(s.get("_summary") or "")[:16000],
                "_judge_scores": s.get("_judge_scores") or {}, "_evidence_window": "historical",
                "runtime_context": s.get("runtime_context") or {},
            }
            projections.append((tenant_id, name, s["session_id"], s["content_hash"], canonical(projection)))
        await conn.executemany(
            f"INSERT INTO {self.schema}.skill_evidence "
            "(tenant_id,skill_name,session_id,content_hash,projection) VALUES ($1,$2,$3,$4,$5::jsonb) "
            "ON CONFLICT (tenant_id,skill_name,session_id) DO NOTHING", projections,
        )

    async def history(self, conn, *, session_id: str = "", limit: int = 20) -> list[dict]:
        rows = await conn.fetch(
            f"SELECT c.command_id,c.created_at,c.finished_at,c.result FROM {self.schema}.skill_commands c "
            "WHERE c.kind='skill.evolve' AND ($1='' OR EXISTS ("
            f"SELECT 1 FROM {self.schema}.skill_evolution_branches b "
            "WHERE b.tenant_id=c.tenant_id AND b.command_id=c.command_id AND $1=ANY(b.session_ids))) "
            "ORDER BY c.created_at DESC LIMIT $2", session_id, min(200, max(1, limit)),
        )
        return [{**dict(r), "result": unpack(r["result"])} for r in rows]
