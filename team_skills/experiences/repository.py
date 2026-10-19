"""PG Experience aggregates and Pattern records; no knowledge-store reads."""
from __future__ import annotations

from team_skills.evolution.repository import canonical, digest, lock_key, unpack
from team_skills.evolution.stages.judge import _parse_skill_experiences
from team_skills.experiences.outbox import ExperienceOutbox
from team_skills.library.repository import SkillLibraryError


class ExperienceRepository:
    def __init__(self, tx):
        self.schema = tx.pool.schema
        self.outbox = ExperienceOutbox(tx)

    async def ingest(self, conn, *, tenant_id, sessions):
        """Project model-authored lessons without invoking another model.

        Reprocessing a Session never increments its occurrence count. Source
        business/ingest times select the description deterministically; importing
        an older Session cannot replace the latest description.
        """
        lessons = [(s, lesson) for s in sessions for lesson in _parse_skill_experiences(s.get("judge") or {})]
        for skill in sorted({lesson["skill_name"] for _, lesson in lessons}):
            await lock_key(conn, tenant_id, f"patterns:{skill}")
        touched = set()
        for session, lesson in sorted(lessons, key=lambda pair: (
            pair[1]["skill_name"], pair[1]["kind"], pair[1]["experience_key"], pair[0]["session_id"],
        )):
            identity = digest([lesson["skill_name"], lesson["kind"], lesson["experience_key"]])
            await lock_key(conn, tenant_id, f"experience:{identity}")
            await conn.execute(
                f"INSERT INTO {self.schema}.skill_experiences "
                "(tenant_id,experience_id,skill_name,kind,experience_key,document,content_hash) "
                "VALUES ($1,$2,$3,$4,$5,'{}','') ON CONFLICT DO NOTHING",
                tenant_id, identity, lesson["skill_name"], lesson["kind"], lesson["experience_key"],
            )
            await conn.execute(
                f"INSERT INTO {self.schema}.experience_occurrences "
                "(tenant_id,experience_id,session_id,user_alias,description,score,observed_at,ingested_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING",
                tenant_id, identity, session["session_id"], str(session.get("user_id") or ""),
                lesson["description"], (session.get("judge") or {}).get("overall_score"),
                session.get("business_time") or session["ingested_at"], session["ingested_at"],
            )
            touched.add(identity)
        for identity in sorted(touched):
            await self._refresh(conn, tenant_id, identity)
        return len(touched)

    async def _refresh(self, conn, tenant_id, identity):
        head = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.skill_experiences WHERE experience_id=$1 FOR UPDATE", identity,
        )
        stats = await conn.fetchrow(
            "SELECT count(*) AS n, min(observed_at) AS first, max(observed_at) AS last, "
            f"max(ingested_at) AS ingested FROM {self.schema}.experience_occurrences WHERE experience_id=$1",
            identity,
        )
        rows = await conn.fetch(
            f"SELECT * FROM {self.schema}.experience_occurrences WHERE experience_id=$1 "
            "ORDER BY observed_at DESC, ingested_at DESC, session_id DESC LIMIT 2000", identity,
        )
        latest = rows[0]
        document = {
            "id": identity, "skill_name": head["skill_name"], "kind": head["kind"],
            "experience_key": head["experience_key"], "description": latest["description"],
            "occurrence_count": stats["n"], "session_ids": sorted(r["session_id"] for r in rows),
            "user_aliases": sorted({r["user_alias"] for r in rows if r["user_alias"]}),
            "first_observed_at": stats["first"].isoformat(), "last_observed_at": stats["last"].isoformat(),
            "last_ingested_at": stats["ingested"].isoformat(), "latest_score": latest["score"],
            "sources_truncated": stats["n"] > len(rows),
        }
        if unpack(head["document"]) == document:
            return
        # Counts are query metadata. Only changed business content creates a
        # new external projection, so more observations do not reindex a file.
        projection = {k: document[k] for k in ("id", "skill_name", "kind", "experience_key", "description")}
        content_hash = digest(projection)
        changed = content_hash != head["content_hash"]
        revision = head["revision"] + int(changed and bool(head["content_hash"]))
        await conn.execute(
            f"UPDATE {self.schema}.skill_experiences SET document=$2::jsonb,content_hash=$3,revision=$4,"
            "updated_at=now() WHERE experience_id=$1",
            identity, canonical(document), content_hash, revision,
        )
        if changed and head["kind"] == "exemplary":
            await self.outbox.append(
                conn, tenant_id=tenant_id, subject_type="experience", subject_id=identity,
                revision=revision, document=projection,
            )

    async def list(self, conn, *, patterns=False, kind="", skill="", status="", search="", limit=50, offset=0):
        table = "experience_patterns" if patterns else "skill_experiences"
        page, start = min(200, max(1, limit)), max(0, offset)
        where = "($1='' OR kind=$1) AND ($2='' OR skill_name=$2) AND ($3='' OR document::text ILIKE $3)"
        args = [kind, skill, f"%{search}%" if search else ""]
        if patterns:
            where += " AND ($4='' OR status=$4)"
            args.append(status)
        total = await conn.fetchval(f"SELECT count(*) FROM {self.schema}.{table} WHERE {where}", *args)
        rows = await conn.fetch(
            f"SELECT document FROM {self.schema}.{table} WHERE {where} "
            f"ORDER BY skill_name,document->>'id',document->>'pattern_id' LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
            *args, page, start,
        )
        groups = await conn.fetch(
            f"SELECT skill_name,kind,count(*) AS n,"
            "sum(COALESCE((document->>'occurrence_count')::int,0)) AS occurrences,"
            "sum(COALESCE((document->>'evidence_count')::int,0)) AS evidence,"
            "count(*) FILTER (WHERE document->>'status'='ready') AS ready "
            f"FROM {self.schema}.{table} GROUP BY skill_name,kind",
        )
        counts = {}
        stats = {}
        for group in groups:
            counts[group["skill_name"]] = counts.get(group["skill_name"], 0) + group["n"]
        if patterns:
            stats = {
                "total_patterns": sum(r["n"] for r in groups), "ready_patterns": sum(r["ready"] for r in groups),
                "observing_patterns": sum(r["n"]-r["ready"] for r in groups),
                "source_experiences": sum(r["evidence"] for r in groups),
                **{f"{k}_patterns": sum(r["n"] for r in groups if r["kind"] == k) for k in ("defect", "exemplary")},
            }
        else:
            stats = {
                "total_experiences": sum(r["n"] for r in groups),
                "total_occurrences": sum(r["occurrences"] for r in groups),
                **{f"{k}_experiences": sum(r["n"] for r in groups if r["kind"] == k)
                   for k in ("defect", "exemplary")},
                **{f"{k}_occurrences": sum(r["occurrences"] for r in groups if r["kind"] == k)
                   for k in ("defect", "exemplary")},
            }
        skills = await conn.fetch(
            f"SELECT DISTINCT skill_name FROM {self.schema}.skill_experiences ORDER BY skill_name",
        )
        return {
            "items": [unpack(r["document"]) for r in rows], "stats": {**stats, "skills": len(counts)},
            "skill_counts": counts, "available_skills": sorted(set(counts) | {r["skill_name"] for r in skills}),
            "total": total, "limit": page, "offset": start, "has_more": start + len(rows) < total,
        }

    async def compile_inputs(self, conn, skill):
        rows = await conn.fetch(
            f"SELECT document FROM {self.schema}.skill_experiences WHERE skill_name=$1 "
            "ORDER BY experience_id LIMIT 501", skill,
        )
        if not rows:
            raise SkillLibraryError("no experiences for this Skill")
        if len(rows) > 500:
            raise SkillLibraryError("pattern compilation exceeds 500 experiences")
        return [unpack(r["document"]) for r in rows]

    async def patterns(self, conn, skill):
        rows = await conn.fetch(
            f"SELECT document FROM {self.schema}.experience_patterns WHERE skill_name=$1 ORDER BY pattern_id", skill,
        )
        return [unpack(r["document"]) for r in rows]

    async def compilation(self, conn, skill):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.experience_pattern_compilations WHERE skill_name=$1", skill,
        )
        return dict(row) if row else None

    async def save_patterns(self, conn, *, tenant_id, skill, source_hash, patterns):
        if len(canonical(patterns).encode()) > 1024 * 1024:
            raise SkillLibraryError("pattern projection exceeds 1 MiB")
        await lock_key(conn, tenant_id, f"patterns:{skill}")
        prior = await self.compilation(conn, skill)
        revision = prior["revision"] + 1 if prior else 1
        now = await conn.fetchval("SELECT clock_timestamp()")
        old = {p["pattern_id"]: p for p in await self.patterns(conn, skill)}
        for p in patterns:
            p["candidate_outcomes"] = old.get(p["pattern_id"], {}).get("candidate_outcomes", [])
            p.update(compiled_at=now.isoformat(), updated_at=now.isoformat())
        await conn.execute(
            f"INSERT INTO {self.schema}.experience_pattern_compilations "
            "(tenant_id,skill_name,source_hash,revision) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (tenant_id,skill_name) DO UPDATE SET source_hash=$3,revision=$4,compiled_at=now()",
            tenant_id, skill, source_hash, revision,
        )
        await conn.execute(f"DELETE FROM {self.schema}.experience_patterns WHERE skill_name=$1", skill)
        await conn.executemany(
            f"INSERT INTO {self.schema}.experience_patterns "
            "(tenant_id,skill_name,pattern_id,kind,status,document) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
            [(tenant_id, skill, p["pattern_id"], p["kind"], p["status"], canonical(p)) for p in patterns],
        )
        await self.outbox.append(
            conn, tenant_id=tenant_id, subject_type="patterns", subject_id=skill, revision=revision,
            document={"skill_name": skill, "source_fingerprint": source_hash, "patterns": patterns},
        )
        return {"compiled": True, "skill_name": skill, "pattern_count": len(patterns), "compiled_at": now.isoformat()}

    async def candidate_outcome(self, conn, *, tenant_id, refs, outcome):
        touched = set()
        for ref in sorted(refs, key=lambda r: (r["skill_name"], r["pattern_id"])):
            skill, identity = ref["skill_name"], ref["pattern_id"]
            await lock_key(conn, tenant_id, f"patterns:{skill}")
            row = await conn.fetchrow(
                f"SELECT document FROM {self.schema}.experience_patterns "
                "WHERE skill_name=$1 AND pattern_id=$2 FOR UPDATE", skill, identity,
            )
            if not row:
                continue
            document = unpack(row["document"])
            if outcome in document.get("candidate_outcomes", []):
                continue
            outcomes = [o for o in document.get("candidate_outcomes", []) if o["job_id"] != outcome["job_id"]]
            document["candidate_outcomes"] = (outcomes + [outcome])[-50:]
            await conn.execute(
                f"UPDATE {self.schema}.experience_patterns SET document=$3::jsonb "
                "WHERE skill_name=$1 AND pattern_id=$2", skill, identity, canonical(document),
            )
            touched.add(skill)
        for skill in sorted(touched):
            prior = await self.compilation(conn, skill)
            await self.save_patterns(
                conn, tenant_id=tenant_id, skill=skill, source_hash=prior["source_hash"],
                patterns=await self.patterns(conn, skill),
            )
