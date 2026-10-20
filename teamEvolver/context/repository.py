"""Incremental Context rows. Every query binds both tenant and user."""

from __future__ import annotations

import hashlib
import json
import secrets


def stable_hash(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class ContextRepository:
    def __init__(self, tx):
        self.tx = tx
        self.schema = tx.pool.schema

    def table(self, name):
        return f"{self.schema}.context_{name}"

    async def audit(self, conn, principal, action, subject):
        await conn.execute(
            f"INSERT INTO {self.table('audit')}(tenant_id,user_id,action,subject_hash) VALUES($1,$2,$3,$4)",
            *self.subject(principal), action, stable_hash(subject))

    @staticmethod
    def subject(principal):
        if not principal.tenant_id or not principal.user_id:
            raise ValueError("Context subject required")
        return principal.tenant_id, principal.user_id

    async def start_session(self, conn, principal, external_id):
        tenant, user = self.subject(principal)
        digest = stable_hash([tenant, user, external_id])[:32]
        sid = "ctxs_" + digest
        created = await conn.fetchval(
            f"INSERT INTO {self.table('sessions')}"
            "(tenant_id,user_id,session_id,external_hash,ov_session_id) VALUES($1,$2,$3,$4,$5) "
            "ON CONFLICT DO NOTHING RETURNING session_id",
            tenant, user, sid, stable_hash(external_id), "agent-" + digest,
        )
        return await self.session(conn, principal, sid, lock=True), created is not None

    async def session(self, conn, principal, sid, *, lock=False):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.table('sessions')} WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3"
            + (" FOR UPDATE" if lock else ""), *self.subject(principal), sid,
        )
        if row is None:
            raise KeyError("context session not found")
        return dict(row)

    async def mark_created(self, conn, principal, sid):
        await conn.execute(
            f"UPDATE {self.table('sessions')} SET ov_created=true,updated_at=now() "
            "WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3", *self.subject(principal), sid,
        )

    async def event_status(self, conn, principal, sid, event_id, sequence, payload):
        session = await self.session(conn, principal, sid, lock=True)
        row = await conn.fetchrow(
            f"SELECT * FROM {self.table('events')} "
            "WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3 AND event_id=$4",
            *self.subject(principal), sid, event_id,
        )
        if row:
            if row["payload_hash"] != stable_hash(payload) or row["sequence"] != sequence:
                raise ValueError("event id reused with different payload")
            return session, True
        if session["committed"]:
            raise ValueError("context session already committed")
        if sequence != session["last_sequence"] + 1:
            raise ValueError(f"context event sequence must be {session['last_sequence'] + 1}")
        return session, False

    async def record_event(self, conn, principal, sid, event_id, sequence, payload):
        await conn.execute(
            f"INSERT INTO {self.table('events')}"
            "(tenant_id,user_id,session_id,event_id,sequence,payload_hash) VALUES($1,$2,$3,$4,$5,$6)",
            *self.subject(principal), sid, event_id, sequence, stable_hash(payload),
        )
        await conn.execute(
            f"UPDATE {self.table('sessions')} SET last_sequence=$4,updated_at=now() "
            "WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3",
            *self.subject(principal), sid, sequence,
        )
        await self.audit(conn, principal, "session.append", [sid, event_id])

    async def commit(self, conn, principal, sid, result):
        await conn.execute(
            f"UPDATE {self.table('sessions')} SET committed=true,result_hash=$4,updated_at=now() "
            "WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3",
            *self.subject(principal), sid, stable_hash(result),
        )
        await self.audit(conn, principal, "session.commit", sid)

    async def issue_ref(self, conn, principal, *, session_id="", scope, uri, kind, version=""):
        if session_id:
            await self.session(conn, principal, session_id)
        rid = "ctx_" + secrets.token_urlsafe(24)
        row = await conn.fetchrow(
            f"INSERT INTO {self.table('refs')}"
            "(tenant_id,user_id,ref_id,session_id,scope,uri,kind,version,expires_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()+interval '15 minutes') RETURNING created_at,expires_at",
            *self.subject(principal), rid, session_id, scope, uri, kind, version,
        )
        return rid, {"tenant_id": principal.tenant_id, "user_id": principal.user_id,
                     "scope": scope, "kind": kind, "version": version, "uri_hash": stable_hash(uri),
                     **{k: v.isoformat() for k, v in row.items()}}

    async def ref(self, conn, principal, rid):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.table('refs')} "
            "WHERE tenant_id=$1 AND user_id=$2 AND ref_id=$3 AND NOT revoked AND expires_at>now()",
            *self.subject(principal), rid,
        )
        return dict(row) if row else None

    async def revoke(self, conn, principal, rid):
        await conn.execute(
            f"UPDATE {self.table('refs')} SET revoked=true WHERE tenant_id=$1 AND user_id=$2 AND ref_id=$3",
            *self.subject(principal), rid,
        )

    async def save_snapshot(self, conn, principal, sid, snapshot_id, items):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           ":".join([*self.subject(principal), snapshot_id]))
        if sid:
            await self.session(conn, principal, sid)
        for item in items:
            ref = await self.ref(conn, principal, item["context_ref"])
            if ref is None or ref["session_id"] != sid:
                raise ValueError("invalid snapshot reference")
        prior = await conn.fetchval(
            f"SELECT session_id FROM {self.table('snapshots')} "
            "WHERE tenant_id=$1 AND user_id=$2 AND snapshot_id=$3",
            *self.subject(principal), snapshot_id)
        existing = await conn.fetch(
            f"SELECT item FROM {self.table('snapshot_items')} "
            "WHERE tenant_id=$1 AND user_id=$2 AND snapshot_id=$3 ORDER BY position",
            *self.subject(principal), snapshot_id)
        if prior is not None:
            if prior != sid or [json.loads(r["item"]) for r in existing] != items:
                raise ValueError("snapshot is immutable")
            return
        await conn.execute(
            f"INSERT INTO {self.table('snapshots')}(tenant_id,user_id,snapshot_id,session_id) "
            "VALUES($1,$2,$3,$4)", *self.subject(principal), snapshot_id, sid)
        for index, item in enumerate(items):
            await conn.execute(
                f"INSERT INTO {self.table('snapshot_items')}"
                "(tenant_id,user_id,snapshot_id,ref_id,position,item) VALUES($1,$2,$3,$4,$5,$6::jsonb) "
                "ON CONFLICT DO NOTHING",
                *self.subject(principal), snapshot_id, item["context_ref"], index, json.dumps(item),
            )
            for level in ("l0", "l1"):
                if item.get(level):
                    await self.record_read(conn, principal, item["context_ref"], level, item[level])
        await self.audit(conn, principal, "snapshot.create", snapshot_id)

    async def record_read(self, conn, principal, rid, level, value):
        if await self.ref(conn, principal, rid) is None:
            raise KeyError("context reference not found")
        await conn.execute(
            f"INSERT INTO {self.table('reads')}(tenant_id,user_id,ref_id,level,value) "
            "VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(tenant_id,user_id,ref_id,level) "
            "DO NOTHING", *self.subject(principal), rid, level, json.dumps(value),
        )
        await self.audit(conn, principal, "context.read", [rid, level])

    async def load_snapshot(self, conn, principal, snapshot_id):
        snapshot = await conn.fetchrow(
            f"SELECT * FROM {self.table('snapshots')} WHERE tenant_id=$1 AND user_id=$2 AND snapshot_id=$3 "
            "AND created_at>now()-interval '15 minutes'",
            *self.subject(principal), snapshot_id,
        )
        if snapshot is None:
            return None
        rows = await conn.fetch(
            f"SELECT i.item,r.uri,r.scope,r.kind,r.version,r.revoked,i.ref_id,r.expires_at>now() AS alive "
            f"FROM {self.table('snapshot_items')} i "
            f"JOIN {self.table('refs')} r USING(tenant_id,user_id,ref_id) "
            "WHERE i.tenant_id=$1 AND i.user_id=$2 AND snapshot_id=$3 ORDER BY position",
            *self.subject(principal), snapshot_id,
        )
        if any(row["revoked"] or not row["alive"] for row in rows):
            return None
        items = []
        for row in rows:
            item = json.loads(row["item"])
            reads = await conn.fetch(
                f"SELECT level,value FROM {self.table('reads')} WHERE tenant_id=$1 AND user_id=$2 AND ref_id=$3",
                *self.subject(principal), row["ref_id"],
            )
            item.update({k: row[k] for k in ("uri", "scope", "kind", "version")})
            item["expanded"] = {r["level"]: json.loads(r["value"]) for r in reads}
            items.append(item)
        return {"snapshot_id": snapshot_id, "subject": {"tenant_id": principal.tenant_id, "user_id": principal.user_id},
                "session_id": snapshot["session_id"], "items": items, "manifest_hash": stable_hash(items)}

    async def usage_refs(self, conn, principal, sid, ref_ids):
        if len(ref_ids) > 200:
            raise ValueError("at most 200 used_context_refs allowed")
        result = []
        for rid in dict.fromkeys(ref_ids):
            ref = await self.ref(conn, principal, rid)
            valid = await conn.fetchval(
                f"SELECT 1 FROM {self.table('snapshot_items')} i JOIN {self.table('snapshots')} s "
                f"USING(tenant_id,user_id,snapshot_id) JOIN {self.table('reads')} r "
                "USING(tenant_id,user_id,ref_id) WHERE i.tenant_id=$1 AND i.user_id=$2 AND i.ref_id=$3 "
                "AND s.session_id=$4 LIMIT 1", *self.subject(principal), rid, sid,
            )
            if not ref or ref["session_id"] != sid or ref["kind"] not in {"memory", "skill"} or not valid:
                raise ValueError("used context reference invalid for session")
            result.append(ref)
        return result

    async def usage_submitted(self, conn, principal, sid, key, *, mark=False):
        if mark:
            await conn.execute(
                f"INSERT INTO {self.table('usage')}(tenant_id,user_id,session_id,usage_key) "
                "VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING", *self.subject(principal), sid, key,
            )
        return bool(await conn.fetchval(
            f"SELECT 1 FROM {self.table('usage')} WHERE tenant_id=$1 AND user_id=$2 AND session_id=$3 AND usage_key=$4",
            *self.subject(principal), sid, key,
        ))
