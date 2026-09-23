"""TE orchestration. OV is accessed only through versioned HTTP contracts."""

import asyncio
import hashlib
import logging
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from teamEvolver.llm import _call_in_pool
from teamEvolver.logging_runtime import event, register_secrets, request_id, safe_code

from .control import ControlStore, canonical
from .diagnostics import diagnostic, failures, upstream_error
from .extractor import extract
from .wire import CandidateBundle, CommitV2, Manifest, PublicationApproval

logger = logging.getLogger(__name__)


class Operations:
    def __init__(self, config, dsn, resolver, transport=None, *, schema="teamevolver"):
        self.config, self.resolve, self.transport = config, resolver, transport
        self.store = ControlStore(dsn, config.queue_limit, schema=schema)
        self.worker = None

    @diagnostic("initialize")
    async def start(self):
        def check_workspace():
            folder = Path(self.config.state_dir).expanduser()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=folder) as probe:
                probe.write(b"probe")
                probe.flush()
            return str(folder.absolute())

        try:
            folder = await asyncio.to_thread(check_workspace)
            event(logger, "ontology.workspace", path=folder, writable=True, schema=self.store.schema)
        except OSError:
            logger.exception("ontology.workspace_unwritable")
            # A workspace failure does not change permissions or disable reads/review.
        await self.store.start()
        event(logger, "ontology.store_ready", schema=self.store.schema)
        self.worker = asyncio.create_task(self.run(), name="te-ontology-worker")
        event(logger, "ontology.worker_started")

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.store.close()
        event(logger, "ontology.worker_stopped")

    @diagnostic("ov", quiet=True)
    async def ov(self, principal, method, path, body=None, *, native=False, identity=None):
        settings = await self.resolve(principal)
        if identity:
            settings = {**settings, "subject": identity}
        register_secrets(settings)
        started = time.monotonic()
        metadata = dict(
            host=urlsplit(settings["url"]).hostname,
            path=path.split("?", 1)[0],
            method=method,
            tenant=principal["tenant"],
            user=principal["subject"],
            ov_account=settings["account"],
            ov_user=settings.get("subject") or principal["subject"],
        )
        failure_key = (principal["tenant"], principal["subject"], metadata["path"])
        headers = {
            "Authorization": "Bearer " + settings["api_key"],
            "X-OpenViking-Account": settings["account"],
            "X-OpenViking-User": settings.get("subject") or principal["subject"],
            "X-OpenViking-Role": "user" if identity else "admin",
            "X-Request-ID": request_id(),
        }
        async with httpx.AsyncClient(timeout=30, trust_env=False, transport=self.transport) as client:
            try:
                async with client.stream(
                    method,
                    settings["url"].rstrip("/") + ("/api/v1" if native else "/api/v1/enterprise") + path,
                    headers=headers,
                    json=body,
                ) as response:
                    chunks, received = [], 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > 64 * 1024 * 1024:
                            raise HTTPException(422, "OV_RESPONSE_SIZE_LIMIT")
                        chunks.append(chunk)
                    r = httpx.Response(response.status_code, headers=response.headers, content=b"".join(chunks))
            except httpx.HTTPError as exc:
                failures.failure(
                    failure_key,
                    "OV_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "OV_NETWORK_ERROR",
                    **metadata,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise HTTPException(503, "OV_UNAVAILABLE_RECONCILE_BY_KEY") from exc
            if r.is_error:
                detail, upstream_code = upstream_error(r, path)
                failures.failure(
                    failure_key,
                    detail,
                    **metadata,
                    upstream_code=upstream_code,
                    status=r.status_code,
                    ov_request_id=r.headers.get("x-request-id", "")[:64],
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise HTTPException(r.status_code, detail)
            failures.success(failure_key, **metadata)
            event(
                logger,
                "ontology.ov_response",
                logging.DEBUG if method == "GET" else logging.INFO,
                **metadata,
                status=r.status_code,
                ov_request_id=r.headers.get("x-request-id", "")[:64],
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
            value = r.json()
            if path == "/ontology/capabilities" and (
                value.get("tenant") != settings["account"]
                or value.get("subject") != (settings.get("subject") or principal["subject"])
            ):
                event(logger, "ontology.identity_mismatch", logging.ERROR, **metadata)
                raise HTTPException(403, "OV_CREDENTIAL_IDENTITY_MISMATCH")
            return value.get("result", value) if native else value

    async def run(self):
        while True:
            try:
                await self.recover()
                job = await self.store.claim()
                if job:
                    event(
                        logger,
                        "ontology.claimed",
                        job_id=job["id"],
                        attempt=job["attempt"],
                        tenant=job["tenant"],
                        user=job["subject"],
                    )
                    try:
                        await asyncio.wait_for(self.execute(job), timeout=600)
                    except asyncio.TimeoutError:
                        current = await self.store.get({"tenant": job["tenant"], "subject": job["subject"]}, job["id"])
                        if job["request"].get("contract", "").startswith("sf.te.ontology."):
                            await self.store.checkpoint(
                                job, current["result"], "failed", "COMPILE_STEP_DEADLINE_EXCEEDED"
                            )
                        else:
                            await self.store.finish(job, error="STEP_DEADLINE_EXCEEDED")

                failures.success(("", "", "worker"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.failure(("", "", "worker"), type(exc).__name__)
            await asyncio.sleep(0.5)

    @diagnostic("retire_legacy_approval")
    async def retire_legacy_approval(self, p, job):
        result = {k: v for k, v in job["result"].items() if k not in {"grant", "approval", "prepared", "commit_key"}}
        result["migration"] = {
            "reason": "PUBLICATION_REAPPROVAL_REQUIRED",
            "previous_commit_key": job["result"].get("commit_key"),
        }
        return await self.store.change(p, job["id"], [job["state"]], "review_ready", result)

    async def recover(self):
        for job in await self.store.recoverable():
            p = {"tenant": job["tenant"], "subject": job["subject"]}
            event(logger, "ontology.reconciling", job_id=job["id"], state=job["state"], tenant=job["tenant"])
            try:
                if job["state"] in {"publishing", "commit_unknown"}:
                    await self.commit(p, job["id"], job["result"]["commit_key"])
                elif job["state"] == "approved":
                    await self.retire_legacy_approval(p, job)
                elif job["state"] == "cancelling":
                    await self.cancel(p, job["id"])
                else:
                    state = "edit_unknown" if job["state"] == "editing" else "review_ready"
                    result = {k: v for k, v in job["result"].items() if k not in {"grant", "approval", "prepared"}}
                    await self.store.change(p, job["id"], [job["state"]], state, result)
            except HTTPException:
                # Retain uncertain state; retries always reconcile before committing.
                continue

    @diagnostic("execute")
    async def execute(self, job):
        if job.get("request", {}).get("contract", "").startswith("sf.te.ontology."):
            from .compile_flow import CompileFlow

            return await CompileFlow(self).step(job)
        p = {"tenant": job["tenant"], "subject": job["subject"]}
        try:
            await self.ov(p, "GET", "/ontology/capabilities")
            await self.ov(p, "POST", "/artifacts/sessions", {"job_id": job["id"], "attempt": job["attempt"]})
            request = job["request"]
            refs = []
            for source in request.get("sources", []):
                refs.append(await self.ov(p, "POST", "/sources", source))
            manifest = Manifest.model_validate(
                {**request["manifest"], **({"sources": refs} if refs else {})}
            ).model_dump(mode="json")
            inputs = await self.ov(p, "POST", "/snapshots/read", manifest)
            event(logger, "ontology.sources_frozen", source_count=len(inputs["sources"]))
            settings = await self.resolve(p)
            folder = (
                Path(self.config.state_dir)
                / hashlib.sha256(canonical(p).encode()).hexdigest()
                / job["id"]
                / str(job["attempt"])
            )
            event(logger, "ontology.extraction_started")
            if not request.get("candidate") and not (self.config.allow_fixture and manifest["extractor"] == "fixture"):
                raise HTTPException(410, "TE_EXTRACTOR_RETIRED_USE_COMPILE")
            bundle = request.get("candidate") or await _call_in_pool(
                extract,
                inputs=inputs,
                workspace=folder,
                model_config=settings["model"],
                allow_fixture=self.config.allow_fixture,
            )
            event(
                logger,
                "ontology.extraction_completed",
                entities=len(bundle.get("entities", [])),
                assertions=len(bundle.get("assertions", [])),
            )
            current = await self.store.get(p, job["id"])
            if current["state"] != "running" or current["attempt"] != job["attempt"]:
                event(logger, "ontology.late_result_fenced", logging.WARNING)
                return {"state": "fenced"}
            artifact = await self.ov(
                p,
                "POST",
                "/artifacts",
                {"job_id": job["id"], "attempt": job["attempt"], "manifest": manifest, "candidate": bundle},
            )
            changed = await self.store.finish(job, {"manifest": manifest, "candidate": bundle, "artifact": artifact})
            return {"state": "review_ready" if changed else "fenced"}
        except asyncio.CancelledError:
            raise  # Lease recovery fences this attempt before a new one uploads.
        except Exception as exc:
            error = str(exc.detail) if isinstance(exc, HTTPException) else type(exc).__name__
            retryable = isinstance(exc, HTTPException) and exc.status_code in {429, 502, 503, 504}
            event(
                logger,
                "ontology.execution_failed",
                logging.ERROR,
                retry=retryable,
                code=safe_code(error, type(exc).__name__),
            )
            logger.exception("ontology.execution_trace")
            await self.store.finish(job, error=error, retry=retryable)
            return {"state": "queued" if retryable and job["attempt"] < 3 else "failed"}

    @diagnostic("edit")
    async def edit(self, p, job_id, candidate, expected_digest):
        job = await self.store.get(p, job_id)
        result = job["result"]
        if result.get("artifact", {}).get("candidate_digest") != expected_digest:
            raise HTTPException(409, "CANDIDATE_CHANGED")
        await self.store.change(p, job_id, ["review_ready", "prepared", "approved", "edit_unknown"], "editing")
        try:
            artifact = await self.ov(
                p,
                "POST",
                "/artifacts",
                {
                    "job_id": job_id,
                    "attempt": job["attempt"],
                    "manifest": result["manifest"],
                    "candidate": CandidateBundle.model_validate(candidate).model_dump(mode="json"),
                },
            )
            updated = {**result, "manifest": result["manifest"], "candidate": candidate, "artifact": artifact}
            for key in ("approval", "prepared", "commit_key", "coverage_acknowledgement"):
                updated.pop(key, None)
            return await self.store.change(p, job_id, ["editing"], "review_ready", updated)
        except Exception:
            # Do not restore stale approval: caller must reconcile/retry the candidate.
            await self.store.change(p, job_id, ["editing"], "edit_unknown")
            raise

    @diagnostic("prepare")
    async def prepare(self, p, job_id):
        job = await self.store.get(p, job_id)
        await self.store.change(p, job_id, ["review_ready", "prepared"], "preparing")
        try:
            result = job["result"]
            prepared = await self.ov(
                p,
                "POST",
                "/assets/prepare",
                {
                    "task_id": result["artifact"]["artifact_id"],
                    "candidate_digest": result["artifact"]["candidate_digest"],
                },
            )
            result["prepared"] = prepared
            return await self.store.change(p, job_id, ["preparing"], "prepared", result)
        except Exception:
            await self.store.change(p, job_id, ["preparing"], "review_ready")
            raise

    @diagnostic("approve")
    async def approve(self, p, job_id, note, acknowledge_gaps=False):
        if not note.strip():
            raise HTTPException(422, "REVIEW_NOTE_REQUIRED")
        job = await self.store.get(p, job_id)
        if job["state"] != "prepared":
            raise HTTPException(409, "PREPARE_REQUIRED")
        # Current OV permission/epoch check; Commit checks them once more.
        caps = await self.ov(p, "GET", "/ontology/capabilities")
        if "approve" not in caps.get("permissions", []):
            raise HTTPException(403, "APPROVAL_PERMISSION_REQUIRED")
        result = job["result"]
        if result.get("gaps") and not acknowledge_gaps:
            raise HTTPException(422, "COVERAGE_ACKNOWLEDGEMENT_REQUIRED")
        artifact = result["artifact"]
        if caps["authorization_epoch"] != artifact["epoch"]:
            raise HTTPException(412, "POLICY_CHANGED")
        if caps["generation"] != artifact["expected_generation"]:
            raise HTTPException(409, "BASE_CHANGED")
        approval = PublicationApproval(
            prepared_id=result["prepared"]["prepared_id"],
            tenant=artifact["tenant"],
            reviewer=caps["subject"],
            candidate_digest=artifact["candidate_digest"],
            manifest_digest=artifact["manifest_digest"],
            expected_generation=artifact["expected_generation"],
            epoch=artifact["epoch"],
            expires_at=int(time.time()) + 300,
            approval_id=secrets.token_hex(16),
            note=note,
        ).model_dump()
        result.pop("grant", None)
        result["approval"] = approval
        result["coverage_acknowledgement"] = {
            "accepted": bool(acknowledge_gaps),
            "reviewer": p["subject"],
            "gaps_digest": hashlib.sha256(canonical(result.get("gaps", [])).encode()).hexdigest(),
            "at": int(time.time()),
        }
        return await self.store.change(p, job_id, ["prepared"], "approved", result)

    @diagnostic("commit")
    async def commit(self, p, job_id, key):
        job = await self.store.get(p, job_id)
        result = job["result"]
        if job["state"] == "published":
            if result.get("commit_key") != key:
                raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
            return job
        if job["state"] not in {"approved", "publishing", "commit_unknown"}:
            raise HTTPException(409, "APPROVAL_REQUIRED")
        if result.get("commit_key") and result["commit_key"] != key:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        result["commit_key"] = key
        await self.store.change(p, job_id, [job["state"]], "publishing", result)
        try:
            from urllib.parse import urlencode

            try:
                receipt = await self.ov(p, "GET", "/assets/commits/by-key?" + urlencode({"key": key}))
                if receipt["prepared_id"] != result["prepared"]["prepared_id"]:
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                if "grant" in result or not result.get("approval", {}).get("approval_id"):
                    return await self.retire_legacy_approval(p, {**job, "state": "publishing"})
                receipt = await self.ov(
                    p,
                    "POST",
                    "/assets/commits",
                    CommitV2(
                        contract="sf.ontology.commit.v2",
                        prepared_id=result["prepared"]["prepared_id"],
                        commit_key=key,
                        approval=result["approval"],
                    ).model_dump(mode="json"),
                )
            result["receipt"] = receipt
            return await self.store.change(p, job_id, ["publishing"], "published", result)
        except Exception:
            await self.store.change(p, job_id, ["publishing"], "commit_unknown", result)
            raise

    @diagnostic("rebase")
    async def rebase(self, p, job_id, key):
        job = await self.store.get(p, job_id)
        caps = await self.ov(p, "GET", "/ontology/capabilities")
        request = dict(job["request"])
        if request.get("contract") == "sf.te.ontology.compile.v1":
            request.update(expected_generation=caps["generation"], submission_key=key)
            return await self.store.submit(p, key, request)
        request["manifest"] = {**request["manifest"], "expected_generation": caps["generation"]}
        request.pop("candidate", None)
        return await self.store.submit(p, key, request)

    @diagnostic("rollback")
    async def rollback(self, p, generation, key):
        material = await self.ov(p, "GET", f"/assets/rollback-material/{generation}")
        return await self.store.submit(p, key, material)

    @diagnostic("cancel")
    async def cancel(self, p, job_id):
        job = await self.store.get(p, job_id)
        if job["state"] == "cancelled":
            return job
        job = await self.store.change(
            p,
            job_id,
            [
                "queued",
                "running",
                "review_ready",
                "prepared",
                "approved",
                "failed",
                "cancelling",
                "edit_unknown",
                "sources_ready",
                "schema_review",
                "compile_unknown",
            ],
            "cancelling",
        )
        if job.get("request", {}).get("contract", "").startswith("sf.te.ontology."):
            from .compile_flow import CompileFlow

            await CompileFlow(self).cancel_remote(p, job)
            if job["request"]["contract"] == "sf.te.ontology.compile.v1":
                await self.ov(p, "POST", "/artifacts/cancel", {"job_id": job_id, "attempt": max(1, job["attempt"])})
            return await self.store.change(p, job_id, ["cancelling"], "cancelled")
        await self.ov(p, "POST", "/artifacts/cancel", {"job_id": job_id, "attempt": max(1, job["attempt"])})
        return await self.store.change(p, job_id, ["cancelling"], "cancelled")
