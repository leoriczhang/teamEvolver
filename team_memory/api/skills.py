"""Account maintenance Skill operations through the platform HTTP client."""
from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.openviking import OpenVikingClient

from ..openviking import get_skill


def create_router(services):
    router = APIRouter()

    def client(request):
        require_tenant_admin(getattr(request.state, "console_user", None))
        from teamEvolver.tenants.registry import effective_config, get_current_tenant
        cfg = effective_config(None, get_current_tenant(), services.extras["config"])
        return OpenVikingClient(endpoint=cfg.sharing_viking_endpoint, api_key=cfg.sharing_viking_api_key,
                                account_id=cfg.sharing_viking_account, user=cfg.sharing_viking_user or "team")

    async def name(stage):
        from teamEvolver.tenants.registry import current_tenant_id
        if stage not in {"aggregation", "maintenance"}:
            raise HTTPException(400, "invalid Skill stage")
        async with services.tx.tenant_tx(current_tenant_id()) as conn:
            module = await services.extras["config_repo"].read_tenant_module(conn, "memory")
        agg = module.config.get("aggregation", module.config)
        key = "okf_skill_uri" if stage == "aggregation" else "maintenance_skill_uri"
        uri = agg.get(key, "viking://agent/skills/team-memory-" + ("okf" if stage == "aggregation" else stage))
        return uri, uri.rsplit("/", 1)[-1]

    @router.post("/api/aggregation/users")
    async def users(request: Request):
        c = client(request)
        try:
            r = await c.request("GET", f"/api/v1/admin/accounts/{c._account_id}/users",
                                params={"limit": 1000, "offset": 0})
            if not r.ok:
                raise HTTPException(r.status_code, "OpenViking user listing unavailable")
            rows = r.data.get("users", []) if isinstance(r.data, dict) else r.data
            return {"account_id": c._account_id, "users": [
                item if isinstance(item, str) else item.get("user_id", item.get("id")) for item in rows]}
        finally:
            await c.aclose()

    @router.get("/api/aggregation/okf-skill")
    async def get(request: Request, stage: str = "aggregation"):
        c = client(request)
        try:
            uri, slug = await name(stage)
            from teamEvolver.openviking.compile import CompileClient
            r = await get_skill(CompileClient(c), slug)
            if not r.ok:
                raise HTTPException(r.status_code, "maintenance Skill unavailable")
            return {"skill_uri": uri, "skill_name": slug, "body": r.data.get("content", ""),
                    "revision": r.data.get("revision", ""), "editable": True, "source": "openviking"}
        finally:
            await c.aclose()

    @router.put("/api/aggregation/okf-skill", status_code=202)
    async def save(request: Request, stage: str = "aggregation"):
        require_tenant_admin(getattr(request.state, "console_user", None))
        raw = await request.json()
        if not isinstance(raw, dict) or not isinstance(raw.get("body"), str) or len(raw["body"].encode()) > 128*1024:
            raise HTTPException(400, "invalid maintenance Skill body")
        uri, slug = await name(stage)
        from team_skills.library.frontmatter import parse_skill_md_text
        parsed = parse_skill_md_text(raw["body"])
        if not parsed or parsed["name"] != slug:
            raise HTTPException(400, "Skill name must match configured URI")
        # A publish is an external mutation; accept a persistent Memory job.
        import uuid

        from teamEvolver.tenants.registry import current_tenant_id

        from ..repository import MemoryRepository
        from ..schemas import run_payload
        tenant = current_tenant_id()
        from teamEvolver.tenants.registry import effective_config, get_current_tenant
        config = effective_config(None, get_current_tenant(), services.extras["config"])
        rid = "memory:" + uuid.uuid4().hex
        async with services.tx.tenant_tx(tenant) as conn:
            module = await services.extras["config_repo"].read_tenant_module(conn, "memory")
            payload = run_payload({}, module, config.sharing_viking_account, config.sharing_viking_user or "team")
            payload["operation"] = "publish_skill"
            payload["skill_uri"], payload["skill_body"] = uri, raw["body"]
            record, _ = await MemoryRepository(services.tx).accept(conn, tenant, rid, raw, payload)
            await services.tasks.enqueue(conn, tenant_id=tenant, task_id=rid, kind="memory.compile",
                                         subject_type="memory_run", subject_id=uri, payload_ref=rid,
                                         payload_hash=record["input_hash"], config_revision=module.revision)
        return {"job_id": rid, "status_url": "/api/jobs/" + rid, "skill_uri": uri}
    return router
