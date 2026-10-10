"""R6 method governance reuses TE jobs, audit and objects; no UpClaw execution integration."""

import copy
import logging
import secrets
import time

from fastapi import HTTPException

from teamEvolver.logging_runtime import event

from .compile import InternalCompile
from .contracts import ProcedurePack, WikiRelease, digest, verify_evidence
from .publish import Publisher
from .sources import Sources
from .store import Structures
from .wiki import page_hashes, render

log = logging.getLogger(__name__)
BUILD = "kee.method-build.v1"


class Governance:
    def __init__(self, ops, reader_mapper=None):
        self.ops = ops
        self.db = Structures(ops.store)
        self.sources = Sources(ops, self.db)
        self.compiler = InternalCompile(ops)
        self.publisher = Publisher(self)
        self.reader_mapper = reader_mapper

    async def map_readers(self, p, readers):
        if self.reader_mapper:
            return await self.reader_mapper(p, readers)
        # Isolated adapters use identical TE/OV subjects; native host supplies authoritative mapping.
        return readers

    async def permissions(self, p, result):
        settings = await self.ops.resolve(p)  # Revalidate tenant/admin/support lifetime on every step.
        if result.get("connection") and result["connection"] != {k: settings[k] for k in ("url", "account")}:
            raise HTTPException(409, "OV_TARGET_CHANGED_REPREPARE_REQUIRED")
        collection = await self.sources.load(p, result["collection_id"])
        if not collection or await self.db.get(p["tenant"], "revoked/" + result["collection_id"]):
            raise HTTPException(403, "SOURCE_REVOKED")
        if not set(result["readers"]) <= set(collection["readers"]):
            raise HTTPException(403, "SOURCE_AUDIENCE_WIDENING")
        return await self.sources.check_current(p, collection, result["readers"])

    async def step(self, job):
        p = {
            "tenant": job["tenant"],
            "subject": job["subject"],
            **job["result"].get("execution_auth_context", job["request"].get("auth_context", {})),
        }
        result = dict(job["result"])
        try:
            if result.get("action") == "publish":
                return await self.publisher.step(p, job)
            if result.get("action") == "withdraw":
                return await self.publisher.withdraw(p, job)
            collection = await self.sources.load(p, job["request"]["collection_id"])
            if not collection or await self.db.get(p["tenant"], "revoked/" + job["request"]["collection_id"]):
                raise HTTPException(403, "SOURCE_REVOKED")
            settings = await self.ops.resolve(p)
            await self.sources.check_current(p, collection)
            pack, usage = await self.compiler.compile(job, collection["sources"], settings["model"])
            # Check the lease and current authorization after the isolated computation too.
            current = await self.ops.store.get(p, job["id"])
            if current["state"] != "running" or current["attempt"] != job["attempt"]:
                return
            result = dict(current["result"])
            candidate_digest = digest(pack)
            await self.db.put(p["tenant"], "packs/" + pack["pack_id"] + "/" + candidate_digest, pack, immutable=True)
            result.update(
                collection_id=job["request"]["collection_id"],
                candidate=pack,
                candidate_digest=candidate_digest,
                schema_confirmed=False,
                readers=collection["readers"],
                clarification=job["request"].get("clarification", ""),
                stage="schema_review",
                executor=self.compiler.executor,
                usage=usage,
                connection={k: settings[k] for k in ("url", "account")},
            )
            await self.ops.store.checkpoint(job, result, "schema_review")
            event(log, "kee.compile_completed", job_id=job["id"], tenant=p["tenant"], calls=usage["calls"])
        except HTTPException as exc:
            current = await self.ops.store.get(p, job["id"])
            if current["state"] != "running" or current["attempt"] != job["attempt"]:
                return
            result = current["result"]
            if result.get("action") == "publish" and exc.status_code in {408, 429, 500, 502, 503, 504}:
                result["retry_at"] = time.time() + 30
                result["last_error"] = str(exc.detail)
                await self.ops.store.checkpoint(job, result, "queued", str(exc.detail))
            else:
                await self.ops.store.checkpoint(job, result, "failed", str(exc.detail))
            event(
                log,
                "kee.step_failed",
                logging.WARNING,
                job_id=job["id"],
                stage=result.get("stage"),
                code=str(exc.detail),
                tenant=p["tenant"],
            )
        except Exception as exc:
            await self.ops.store.checkpoint(job, result, "failed", type(exc).__name__)
            event(log, "kee.step_failed", logging.ERROR, job_id=job["id"], code=type(exc).__name__)

    async def edit(self, p, job_id, pack, expected):
        job = await self.ops.store.get(p, job_id)
        result = copy.deepcopy(job["result"])
        if expected != result.get("candidate_digest"):
            raise HTTPException(409, "CANDIDATE_DIGEST_CONFLICT")
        collection = await self.sources.load(p, result["collection_id"])
        try:
            verify_evidence(pack, collection["sources"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        payload = pack.model_dump(mode="json")
        result.update(candidate=payload, candidate_digest=digest(payload), schema_confirmed=False)
        for key in ("prepared", "approval", "release", "commit_key", "action", "verified_pages"):
            result.pop(key, None)
        await self.db.put(p["tenant"], "packs/" + pack.pack_id + "/" + digest(payload), payload, immutable=True)
        return await self.ops.store.change(
            p, job_id, ["schema_review", "review_ready", "prepared", "approved"], "schema_review", result
        )

    async def confirm(self, p, job_id, expected):
        job = await self.ops.store.get(p, job_id)
        if expected != job["result"].get("candidate_digest"):
            raise HTTPException(409, "CANDIDATE_DIGEST_CONFLICT")
        result = {**job["result"], "schema_confirmed": True, "stage": "review"}
        return await self.ops.store.change(p, job_id, ["schema_review"], "review_ready", result)

    async def prepare(self, p, job_id, target, readers):
        job = await self.ops.store.get(p, job_id)
        result = dict(job["result"])
        if not result.get("schema_confirmed"):
            raise HTTPException(409, "SCHEMA_CONFIRMATION_REQUIRED")
        result["readers"] = readers
        epoch = await self.permissions(p, result)
        head = await self.db.get(p["tenant"], "heads/" + digest(target), default={})
        if head.get("pending_job"):
            raise HTTPException(409, "WIKI_TARGET_BUSY")
        pack = ProcedurePack.model_validate(result["candidate"])
        from .references import validate_references

        await validate_references(self.db, p["tenant"], pack)
        pages = render(pack)
        result["prepared"] = {
            "id": secrets.token_hex(16),
            "candidate_digest": result["candidate_digest"],
            "collection_id": result["collection_id"],
            "baseline": head.get("digest", ""),
            "target_uri": target,
            "pages": page_hashes(pages),
            "auth_state": epoch,
            "readers": readers,
        }
        result.pop("approval", None)
        return await self.ops.store.change(
            p, job_id, ["review_ready", "prepared", "approved", "imported"], "prepared", result
        )

    async def approve(self, p, job_id, note, acknowledge_gaps=False):
        job = await self.ops.store.get(p, job_id)
        result = dict(job["result"])
        prepared = result.get("prepared")
        if not prepared or job["state"] != "prepared":
            raise HTTPException(409, "PREPARE_REQUIRED")
        if result["candidate"].get("gaps") and not acknowledge_gaps:
            raise HTTPException(409, "COVERAGE_ACKNOWLEDGEMENT_REQUIRED")
        if await self.permissions(p, result) != prepared["auth_state"]:
            raise HTTPException(409, "AUTHORIZATION_CHANGED")
        result["approval"] = {
            "approval_id": secrets.token_hex(16),
            "reviewer": p["subject"],
            "prepared_digest": digest(prepared),
            "candidate_digest": result["candidate_digest"],
            "expires_at": time.time() + 300,
            "note": note,
            "gaps_acknowledged": acknowledge_gaps,
        }
        return await self.ops.store.change(p, job_id, ["prepared"], "approved", result)

    async def commit(self, p, job_id, key):
        job = await self.ops.store.get(p, job_id)
        result = dict(job["result"])
        if result.get("commit_key"):
            if result["commit_key"] != key:
                raise HTTPException(409, "COMMIT_KEY_CONFLICT")
            return job
        approval = result.get("approval", {})
        if (
            job["state"] != "approved"
            or approval.get("expires_at", 0) <= time.time()
            or approval.get("candidate_digest") != result.get("candidate_digest")
            or approval.get("prepared_digest") != digest(result.get("prepared"))
        ):
            raise HTTPException(409, "VALID_APPROVAL_REQUIRED")
        await self.permissions(p, result)
        prepared = result["prepared"]
        release = WikiRelease(
            pack_digest=result["candidate_digest"],
            target_uri=prepared["target_uri"],
            baseline=prepared["baseline"],
            pages=prepared["pages"],
            sources=[result["collection_id"]],
            approval_id=approval["approval_id"],
        )
        result["execution_auth_context"] = {k: p[k] for k in ("support_id", "service_root") if p.get(k)}
        result.update(
            release=release.model_dump(mode="json"),
            commit_key=key,
            action="publish",
            stage="wiki_publish",
            publish_started=time.time(),
            publish_deadline=time.time() + 1800,
        )
        return await self.ops.store.resume(p, job_id, ["approved"], result)

    async def retry(self, p, job_id):
        job = await self.ops.store.get(p, job_id)
        result = dict(job["result"])
        if result.get("action") == "publish" and result.get("publish_deadline", 0) <= time.time():
            raise HTTPException(409, "WIKI_DEADLINE_MANUAL_RECONCILIATION_REQUIRED")
        result.pop("retry_at", None)
        result["execution_auth_context"] = {k: p[k] for k in ("support_id", "service_root") if p.get(k)}
        return await self.ops.store.resume(p, job_id, ["failed"], result)

    async def cancel(self, p, job_id):
        job = await self.ops.store.get(p, job_id)
        if job["result"].get("release"):
            # Partial publication requires cleanup rather than clearing its durable target intent.
            raise HTTPException(409, "PUBLICATION_REQUIRES_WITHDRAWAL")
        await self.compiler.cancel(job)
        return await self.ops.store.change(
            p,
            job_id,
            ["queued", "running", "failed", "schema_review", "review_ready", "prepared", "approved"],
            "cancelled",
        )

    async def withdraw(self, p, job_id):
        job = await self.ops.store.get(p, job_id)
        if job["state"] == "withdrawn":
            return job
        result = dict(job["result"])
        if not result.get("release"):
            raise HTTPException(409, "NO_WIKI_RELEASE")
        result.update(
            action="withdraw",
            stage="wiki_withdraw",
            execution_auth_context={k: p[k] for k in ("support_id", "service_root") if p.get(k)},
        )
        return await self.ops.store.resume(p, job_id, ["published", "failed", "queued"], result)
