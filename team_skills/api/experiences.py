"""Experience/Pattern queries and short, durable command acceptance."""
from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from team_skills.api.candidates import accepted, actor, command_id, error
from team_skills.api.common import read_json
from team_skills.config import load_skills_config
from team_skills.experiences.commands import ExperienceCommands
from team_skills.experiences.patterns import source_fingerprint
from team_skills.experiences.repository import ExperienceRepository
from team_skills.experiences.supervisor import accept_scan
from team_skills.experiences.transport import ROOT, target_config
from team_skills.library.repository import SkillLibraryError
from teamEvolver.config_runtime import TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id


def create_router(services):
    router = APIRouter(tags=["experiences"])
    tx, configs = services.tx, services.extras["config_repo"]
    repo, commands = ExperienceRepository(tx), ExperienceCommands(services)

    async def listing(patterns, kind, skill, search, limit, offset, status=""):
        if kind not in {"", "defect", "exemplary"} or status not in {"", "ready", "observing"}:
            raise HTTPException(400, "invalid experience kind or pattern status")
        if len(search) > 500 or len(skill) > 200:
            raise HTTPException(400, "experience filter is too long")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return await repo.list(
                conn, patterns=patterns, kind=kind, skill=skill, search=search,
                limit=limit, offset=offset, status=status,
            )

    @router.get("/api/skill-experiences")
    async def experiences(kind: str = "", skill: str = "", search: str = "", limit: int = 50, offset: int = 0):
        return await listing(False, kind, skill, search, limit, offset)

    @router.get("/api/skill-experience-patterns")
    async def patterns(kind: str = "", skill: str = "", search: str = "", status: str = "",
                       limit: int = 50, offset: int = 0):
        return await listing(True, kind, skill, search, limit, offset, status)

    @router.post("/api/skill-experience-patterns/compile", status_code=202)
    async def compile_patterns(request: Request):
        actor(request)
        body = await read_json(request)
        if (set(body) - {"skill", "force"} or not isinstance(body.get("skill"), str)
                or not body["skill"].strip() or len(body["skill"]) > 200
                or type(body.get("force", False)) is not bool):
            raise HTTPException(400, "compile requires skill and optional boolean force")
        tid, cid = current_tenant_id(), command_id(request, "pattern-compile")
        try:
            async with tx.tenant_tx(tid) as conn:
                config = await load_skills_config(configs, conn)
                existing = await commands.get(conn, cid)
                if existing:
                    payload = existing["payload"]
                else:
                    inputs = await repo.compile_inputs(conn, body["skill"])
                    payload = {"skill": body["skill"], "force": body.get("force", False),
                               "experiences": inputs, "source_hash": source_fingerprint(inputs)}
                command = await commands.accept(
                    conn, tenant_id=tid, command_id=cid, operation="compile", request=body,
                    payload=payload, config_revision=config.revision,
                )
            return accepted(command)
        except (SkillLibraryError, TenantConfigMissing) as exc:
            raise error(exc) from exc

    @router.get("/api/experience-sync/status")
    async def status(request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(configs, conn)
                result = await repo.outbox.status(conn)
                manual = await commands.latest_scan(conn)
                last_scan = await conn.fetchval(f"SELECT updated_at FROM {repo.schema}.experience_scan_state")
            settings = config.get("experience_sync") or {}
            return {**result, "enabled": settings.get("enabled", False),
                    "target_directory": settings.get("target_directory", ROOT), "manual": manual,
                    "last_scan": last_scan.timestamp() if last_scan else None}
        except TenantConfigMissing as exc:
            raise error(exc) from exc

    async def scan(request, operation):
        actor(request)
        if await read_json(request):
            raise HTTPException(400, "experience sync takes no body or identity/config overrides")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                config = await load_skills_config(configs, conn)
                if not (config.get("experience_sync") or {}).get("enabled", False):
                    raise HTTPException(409, "SYNC_DISABLED")
                target_config(config)
                command = await accept_scan(
                    conn, services, tenant_id=current_tenant_id(), command_id=command_id(request, "experience-scan"),
                    operation=operation, config=config,
                )
            return accepted(command)
        except (SkillLibraryError, TenantConfigMissing) as exc:
            raise error(exc) from exc

    @router.post("/api/experience-sync/trigger", status_code=202)
    async def trigger(request: Request):
        return await scan(request, "sync")

    @router.post("/api/experience-sync/import", status_code=202)
    async def import_all(request: Request):
        return await scan(request, "import_all")

    @router.get("/api/experience-sync/jobs/{job_id}/result")
    async def result(job_id: str):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            command = await commands.get(conn, job_id)
        if command is None:
            raise HTTPException(404, "experience command not found")
        return {"job_id": job_id, "result": command["result"]}

    return router
