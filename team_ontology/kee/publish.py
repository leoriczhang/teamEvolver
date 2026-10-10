"""Resumable Wiki upload. KEE CAS is local; OV does not provide package-atomic commits."""

import hashlib
import time
from urllib.parse import urlencode

from fastapi import HTTPException

from .contracts import ProcedurePack, digest
from .wiki import render


class Publisher:
    def __init__(self, service):
        self.service = service
        self.ops, self.db = service.ops, service.db

    async def native(self, p, method, path, body=None):
        return await self.ops.ov(p, method, path, body, native=True)

    async def read(self, p, uri):
        try:
            value = await self.native(p, "GET", "/content/read?" + urlencode({"uri": uri}))
            return value if isinstance(value, str) else value.get("content", "")
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise

    async def directory(self, p, uri):
        parts = uri.removeprefix("viking://resources/").split("/")
        for index in range(1, len(parts) + 1):
            path = "viking://resources/" + "/".join(parts[:index])
            try:
                await self.native(p, "POST", "/fs/mkdir", {"uri": path})
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                stat = await self.native(p, "GET", "/fs/stat?" + urlencode({"uri": path}))
                if stat.get("isDir") is not True:
                    raise HTTPException(409, "WIKI_PATH_TYPE_CONFLICT") from None

    async def acl(self, p, uri, readers, private=False):
        settings = await self.ops.resolve(p)
        owner = settings.get("subject") or p["subject"]
        identities = await self.service.map_readers(p, readers)
        entries = [{"principal": "user:" + owner, "level": "manage"}]
        if not private:
            entries += [{"principal": "user:" + user, "level": "read"} for user in identities if user != owner]
        await self.native(p, "PUT", "/acl", {"uri": uri, "acl_mode": "restricted", "entries": entries})
        value = await self.native(p, "GET", "/acl?" + urlencode({"uri": uri}))
        # Must fail closed when the account ignores ACL.
        mode = value.get("acl_mode", value.get("mode"))
        if mode != "restricted":
            raise HTTPException(403, "WIKI_ACL_NOT_ENFORCED")
        probe = "kee-denied-probe-" + digest({"uri": uri})[:16]
        try:
            await self.ops.ov(p, "GET", "/fs/stat?" + urlencode({"uri": uri}), native=True, identity=probe)
        except HTTPException as exc:
            if exc.status_code == 403:
                return
            raise
        raise HTTPException(403, "WIKI_ACL_PROBE_READABLE")

    async def ensure_text(self, p, uri, text, mutable=False):
        previous = await self.read(p, uri)
        if previous == text:
            return {"reconciled": True}
        if previous is not None and not mutable:
            raise HTTPException(409, "WIKI_IMMUTABLE_CONTENT_CONFLICT")
        result = await self.native(
            p,
            "POST",
            "/content/write",
            {"uri": uri, "content": text, "mode": "replace" if previous is not None else "create", "wait": False},
        )
        if await self.read(p, uri) != text:
            raise HTTPException(409, "WIKI_READBACK_MISMATCH")
        return result

    async def index(self, p, uri):
        # Scoped readback first; global queue drain is conservative (unrelated failure blocks success).
        result = await self.native(p, "POST", "/system/wait", {"timeout": 5})
        if has_failures(result):
            raise HTTPException(503, "WIKI_INDEX_NOT_READY")
        consistency = await self.native(p, "POST", "/system/consistency", {"uri": uri})
        if consistency.get("ok") is not True or consistency.get("missing_record_count", -1) != 0:
            raise HTTPException(503, "WIKI_INDEX_NOT_CONFIRMED")
        return consistency

    async def step(self, p, job):
        result = dict(job["result"])
        release = dict(result["release"])
        if result.get("publish_deadline", float("inf")) <= time.time():
            raise HTTPException(409, "WIKI_DEADLINE_MANUAL_RECONCILIATION_REQUIRED")
        target = release["target_uri"]
        headkey = "heads/" + digest(target)
        pack = ProcedurePack.model_validate(result["candidate"])
        pages = render(pack)
        root = target + "/releases/" + release["pack_digest"]
        # One existing job owns a target until it finishes or is explicitly cancelled/reconciled.
        async with self.db.transaction(p["tenant"], target) as c:
            head = await self.db.get(p["tenant"], headkey, connection=c, default={})
            if head.get("pending_job") not in {None, job["id"]}:
                raise HTTPException(409, "WIKI_TARGET_BUSY")
            if head.get("digest", "") != release["baseline"] and head.get("digest") != release["pack_digest"]:
                raise HTTPException(409, "WIKI_BASELINE_CONFLICT_REAPPROVAL_REQUIRED")
            await self.db.put(p["tenant"], headkey, {**head, "pending_job": job["id"]}, connection=c)
        stage = release["state"]
        if stage in {"private", "uploading", "index_wait", "entry_unknown"}:
            await self.service.permissions(p, result)
        if stage == "private":
            await self.directory(p, root)
            await self.acl(p, root, result["readers"], private=True)
            release["state"] = "uploading"
        elif stage == "uploading":
            verified = set(result.get("verified_pages", []))
            remaining = [key for key in pages if key not in verified]
            # Bound HTTP work per lease; checkpoints fence late responses and avoid two schedulers.
            for name in remaining[:4]:
                parent = (root + "/" + name).rsplit("/", 1)[0]
                await self.directory(p, parent)
                written = await self.ensure_text(p, root + "/" + name, pages[name])
                if written.get("task_id"):
                    release.setdefault("processing_task_ids", []).append(written["task_id"])
                verified.add(name)
                result["verified_pages"] = sorted(verified)
                result["release"] = release
                job = await self.ops.store.checkpoint(job, result, "running")
            if not [key for key in pages if key not in verified]:
                release["state"] = "index_wait"
        elif stage == "index_wait":
            await self.index(p, root)
            # Recheck all immutable content before exposing this release.
            for name, checksum in release["pages"].items():
                text = await self.read(p, root + "/" + name)
                if text is None or hashlib.sha256(text.encode()).hexdigest() != checksum:
                    raise HTTPException(409, "WIKI_READBACK_MISMATCH")
            await self.acl(p, root, result["readers"])
            release["state"] = "entry_unknown"
        elif stage == "entry_unknown":
            async with self.db.transaction(p["tenant"], target) as c:
                current = await c.fetchrow(
                    f"SELECT state,attempt FROM {self.ops.store.jobs} WHERE id=$1 FOR UPDATE", job["id"]
                )
                if not current or current["state"] != "running" or current["attempt"] != job["attempt"]:
                    raise HTTPException(409, "WIKI_EXECUTION_FENCED")
                head = await self.db.get(p["tenant"], headkey, connection=c, default={})
                if head.get("pending_job") != job["id"]:
                    raise HTTPException(409, "WIKI_TARGET_FENCED")
                entry = f"# 当前方法 Wiki\n\n[阅读已批准版本](releases/{release['pack_digest']}/index.md)\n"
                await self.ensure_text(p, target + "/index.md", entry, mutable=True)
                await self.acl(p, target + "/index.md", result["readers"])
                await self.index(p, root)
                receipt = {
                    "commit_key": result["commit_key"],
                    "job_id": job["id"],
                    "pack_digest": release["pack_digest"],
                    "entry_uri": target + "/index.md",
                    "release_uri": root,
                    "published_at": time.time(),
                    "package_atomic": False,
                }
                release.update(state="published", receipt=receipt)
                result["release"] = release
                await self.db.put(
                    p["tenant"], headkey, {"digest": release["pack_digest"], "job_id": job["id"]}, connection=c
                )
                await self.db.put(
                    p["tenant"],
                    "releases/" + digest(target) + "/" + release["pack_digest"],
                    {"release": release, "readers": result["readers"], "candidate": result["candidate"]},
                    connection=c,
                )
                from .contracts import canonical

                await c.execute(
                    f"UPDATE {self.ops.store.jobs} SET state='published',result=$2::jsonb,updated=now() WHERE id=$1",
                    job["id"],
                    canonical(result),
                )
                await c.execute(f"UPDATE {self.ops.store.outbox} SET pending=false WHERE job_id=$1", job["id"])
                await self.ops.store.audit(p, "kee.wiki.published", receipt, c)
            return
        result["release"] = release
        await self.ops.store.checkpoint(job, result, "queued")

    async def withdraw(self, p, job):
        result = dict(job["result"])
        release = dict(result["release"])
        root = (
            release.get("receipt", {}).get("release_uri")
            or release["target_uri"] + "/releases/" + release["pack_digest"]
        )
        target = release["target_uri"]
        # Deny first; then remove entire release (pages, sources, backlinks, indexes), not only the pointer.
        try:
            await self.native(p, "GET", "/fs/stat?" + urlencode({"uri": root}))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            await self.acl(p, root, [], private=True)
        try:
            await self.native(p, "DELETE", "/fs/rm?" + urlencode({"uri": root, "recursive": "true"}))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        headkey = "heads/" + digest(target)
        async with self.db.transaction(p["tenant"], target) as c:
            head = await self.db.get(p["tenant"], headkey, connection=c, default={})
            if head.get("pending_job") in {job["id"], job["request"].get("revocation_of")} and head.get("pending_job"):
                head.pop("pending_job", None)
                await self.db.put(p["tenant"], headkey, head, connection=c)
            if head.get("digest") == release["pack_digest"]:
                await self.ensure_text(p, target + "/index.md", "# 当前方法 Wiki\n\n该版本已撤回。\n", mutable=True)
                await self.db.put(
                    p["tenant"], headkey, {"digest": "", "withdrawn": release["pack_digest"]}, connection=c
                )
        # OV removal must remove semantic/vector artifacts; query denial is checked independently.
        try:
            await self.native(p, "GET", "/fs/stat?" + urlencode({"uri": root}))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            raise HTTPException(503, "WIKI_WITHDRAW_NOT_CONFIRMED")
        release["state"] = "withdrawn"
        release.setdefault("receipt", {})["withdrawn_at"] = time.time()
        result["release"] = release
        await self.db.put(
            p["tenant"],
            "releases/" + digest(target) + "/" + release["pack_digest"],
            {"release": release, "readers": result["readers"], "candidate": result["candidate"]},
        )
        await self.ops.store.checkpoint(job, result, "withdrawn")


def has_failures(value):
    if isinstance(value, dict):
        return any(
            (k in {"error", "errors", "failed", "failed_count"} and bool(v)) or has_failures(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(has_failures(v) for v in value)
    return False
