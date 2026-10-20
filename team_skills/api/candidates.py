"""Candidate review API. Expensive evaluation and publish run on the Worker."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from team_skills.api.common import read_json
from team_skills.candidates.repository import CandidateRepository
from team_skills.config import load_skills_config
from team_skills.evolution.candidate_pipeline import enqueue_command, submit_validation
from team_skills.evolution.history import candidate_payload, feedback
from team_skills.evolution.repository import EvolutionRepository
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library import frontmatter
from team_skills.library.repository import SkillLibraryError
from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id


def actor(request):
    user = getattr(request.state, "console_user", None)
    require_tenant_admin(user)
    return str(user.get("id") or user.get("username") or "")


def command_id(request, prefix):
    key = request.headers.get("idempotency-key", "")
    if len(key) > 200:
        raise HTTPException(400, "Idempotency-Key is too long")
    return prefix + ":" + (hashlib.sha256(key.encode()).hexdigest() if key else uuid.uuid4().hex)


def accepted(command):
    return JSONResponse(status_code=202, content={
        "status": "accepted", "job_id": command["command_id"],
        "status_url": f"/api/jobs/{command['command_id']}",
    })


def error(exc):
    return HTTPException(503 if isinstance(exc, TenantConfigMissing) else exc.status_code, str(exc))


def create_router(services, **kwargs):
    router = APIRouter(tags=["skill-evolution"])
    tx = services.tx
    repo = CandidateRepository(tx)
    config_repo = services.extras["config_repo"]

    @router.get("/api/skill-candidates")
    async def listing(scope: str = "open", limit: int = 20, offset: int = 0, compact: bool = False):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                rows, total = await repo.list(conn, scope=scope, limit=limit, offset=offset)
                page = [await candidate_payload(conn, tx, row, compact=compact) for row in rows]
        except SkillLibraryError as exc:
            raise error(exc) from exc
        return {"candidates": page, "total": total, "limit": min(200, max(1, limit)),
                "offset": max(0, offset), "has_more": max(0, offset) + len(page) < total}

    @router.get("/api/skill-candidates/{candidate_id}/detail")
    async def detail(candidate_id: str):
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await candidate_payload(conn, tx, await repo.require(conn, candidate_id))
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.patch("/api/skill-candidates/{candidate_id}/feedback")
    async def mark(candidate_id: str, request: Request):
        reviewer = actor(request)
        body = await read_json(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                item = await repo.set_marks(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id, changes=body, actor=reviewer,
                )
                return {"job_id": candidate_id, "feedback": feedback(item, await repo.events(conn, candidate_id))}
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.put("/api/skill-candidates/{candidate_id}/content")
    async def edit(candidate_id: str, request: Request):
        reviewer = actor(request)
        body = await read_json(request)
        if set(body) - {"name", "description", "category", "content", "candidate_revision"}:
            raise HTTPException(400, "unknown candidate content field")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(config_repo, conn)
                item = await repo.require(conn, candidate_id, for_update=True)
                if "name" in body and body["name"] != item["skill_name"]:
                    raise HTTPException(400, "candidate skill cannot be renamed")
                bundle = await repo.read_bundle(conn, candidate_id)
                split = frontmatter.split_skill_md(bundle["SKILL.md"].decode())
                fields = frontmatter._load_frontmatter_from_raw(bundle["SKILL.md"].decode())
                bundle["SKILL.md"] = frontmatter.render_skill_md(
                    name=item["skill_name"], description=body.get("description", fields["description"]),
                    category=body.get("category", fields.get("category", "general")),
                    content=body.get("content", split[1]),
                    extra_frontmatter={k: v for k, v in fields.items() if k not in {"name", "description", "category"}},
                ).encode()
                changed = await repo.revise_content(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id, bundle=bundle,
                    limits=config.bundle_limits, actor=reviewer, expected_revision=body.get("candidate_revision"),
                )
                return await candidate_payload(conn, tx, changed)
        except (SkillLibraryError, TenantConfigMissing) as exc:
            raise error(exc) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    async def submit(candidate_id, request, mode, revision=None):
        reviewer = actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(config_repo, conn)
                command = await submit_validation(
                    conn, services, tenant_id=current_tenant_id(), candidate_id=candidate_id,
                    command_id=command_id(request, "validate"), config=config, actor=reviewer,
                    revision=revision, mode=mode,
                )
            return accepted(command)
        except (SkillLibraryError, TenantConfigMissing) as exc:
            raise error(exc) from exc

    @router.post("/api/skill-candidates/{candidate_id}/evaluate")
    async def evaluate(candidate_id: str, request: Request):
        return await submit(candidate_id, request, "evaluate")

    @router.post("/api/skill-candidates/{candidate_id}/validate")
    async def validate(candidate_id: str, request: Request):
        body = await read_json(request)
        if set(body) - {"mode", "candidate_revision"} or body.get("mode", "auto") not in {"auto", "force"}:
            raise HTTPException(400, "validation requires mode auto or force and optional candidate_revision")
        return await submit(candidate_id, request, body.get("mode", "auto"), body.get("candidate_revision"))

    @router.post("/api/skill-candidates/{candidate_id}/reject")
    async def reject(candidate_id: str, request: Request):
        reviewer = actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                item = await repo.require(conn, candidate_id, for_update=True)
                if item["status"] != "rejected":
                    item = await repo.transition(
                        conn, tenant_id=current_tenant_id(), candidate_id=candidate_id, to_status="rejected",
                        actor=reviewer, reason="manual review",
                        decision={"status": "rejected", "mode": "manual", "accepted": False},
                    )
                    await ExperienceRepository(tx).candidate_outcome(
                        conn, tenant_id=current_tenant_id(), refs=item["detail"].get("pattern_refs") or [],
                        outcome={"job_id": candidate_id, "candidate_revision": item["revision"], "status": "rejected"},
                    )
                return {"status": "rejected", "feedback": feedback(item, await repo.events(conn, candidate_id))}
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-evolution/trigger")
    async def trigger(request: Request):
        reviewer = actor(request)
        body = await read_json(request)
        ids = body.get("session_ids", [])
        if set(body) - {"session_ids"} or not isinstance(ids, list) or len(ids) > 200:
            raise HTTPException(400, "trigger accepts at most 200 session_ids; no identity/config overrides")
        if any(not isinstance(value, str) or not value for value in ids):
            raise HTTPException(400, "session_ids must contain nonempty strings")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(config_repo, conn)
                now = await conn.fetchval("SELECT clock_timestamp()")
                command, _ = await enqueue_command(
                    conn, services, tenant_id=current_tenant_id(), command_id=command_id(request, "evolve"),
                    kind="skill.evolve", request=body, payload={
                        "session_ids": ids, "actor": reviewer, "config": config.config,
                        "config_revision": config.revision, "accepted_at": now.isoformat(),
                    },
                )
            return accepted(command)
        except (SkillLibraryError, TenantConfigMissing) as exc:
            raise error(exc) from exc

    @router.get("/history")
    async def history(session_id: str = "", limit: int = 20):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await EvolutionRepository(tx).history(conn, session_id=session_id, limit=limit)
        return {"reachable": True, "cycles": rows}

    @router.get("/api/skill-evolution/jobs/{job_id}/result")
    async def result(job_id: str):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            command = await EvolutionRepository(tx).command(conn, job_id)
        if command is None:
            raise HTTPException(404, "skill command not found")
        return {"job_id": job_id, "result": command["result"]}

    return router
