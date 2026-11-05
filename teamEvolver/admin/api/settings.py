"""Online console settings on versioned PG configuration and encrypted secrets."""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from ...security import SecretBox, SecretRepository
from ...tenants.registry import current_tenant_id
from ..authorization import require_platform_admin, require_tenant_admin


def secret_repository(tx):
    master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
    if not master:
        raise HTTPException(503, "configure TEAMEVOLVER_SECRET_MASTER_KEY before storing credentials")
    return SecretRepository(tx, SecretBox(master))


def create_router(services):
    router = APIRouter(tags=["settings"])
    tx, repo = services.tx, services.extras["config_repo"]
    cfg = services.extras["config"]

    async def body(request):
        from session_ingestion.http import read_limited_json_body
        return await read_limited_json_body(request)

    def authorize(request, platform=False):
        checker = require_platform_admin if platform else require_tenant_admin
        checker(getattr(request.state, "console_user", None))

    async def read_model(conn):
        row = await repo.read_tenant_module(conn, "skills")
        model = dict(row.config.get("model", {}))
        return row, {
            "base_url": model.get("base_url", cfg.llm_api_base), "model": model.get("model", cfg.llm_model_id),
            "temperature": model.get("temperature", cfg.llm_temperature),
            "max_tokens": model.get("max_tokens", cfg.llm_max_tokens),
            "api_key_present": bool(model.get("secret_ref") or cfg.llm_api_key),
            "scope": "tenant", "tenant_id": current_tenant_id(), "revision": row.revision,
        }

    async def model_update(conn, raw):
        if set(raw) - {"model", "base_url", "temperature", "max_tokens", "api_key", "clear_api_key",
                       "scope", "tenant_id", "revision", "api_key_present"}:
            raise HTTPException(400, "unsupported model setting")
        row, _ = await read_model(conn)
        value = dict(row.config.get("model", {}))
        value.update({k: raw[k] for k in ("model", "base_url", "temperature", "max_tokens") if k in raw})
        if raw.get("clear_api_key"):
            value.pop("secret_ref", None)
        if raw.get("api_key"):
            reference = await secret_repository(tx).store(
                conn, tenant_id=current_tenant_id(), plaintext=raw["api_key"], purpose="llm")
            value["secret_ref"] = reference.ref
        return row, value

    @router.get("/api/model-settings")
    async def model(request: Request):
        authorize(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            _, value = await read_model(conn)
        return value

    @router.put("/api/model-settings")
    async def model_save(request: Request):
        authorize(request)
        raw = await body(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row, value = await model_update(conn, raw)
            await repo.write_tenant_module(conn, module="skills", expected_revision=raw.get("revision", row.revision),
                                          config={**row.config, "model": value})
            _, result = await read_model(conn)
        return result

    @router.post("/api/model-settings/test", status_code=202)
    async def model_test(request: Request):
        authorize(request)
        raw = await body(request)
        job = "model-test:" + uuid.uuid4().hex
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row, model = await model_update(conn, raw)
            from team_skills.evolution.candidate_pipeline import enqueue_command
            await enqueue_command(conn, services, tenant_id=current_tenant_id(), command_id=job,
                                  kind="skill.evolve", request={"model": model},
                                  payload={"operation": "model_test", "config_revision": row.revision,
                                           "config": row.config, "model": model})
        return {"job_id": job, "status_url": f"/api/jobs/{job}"}

    async def global_read():
        async with tx.pool.admin_conn() as conn:
            return await repo.read_global(conn)

    async def global_write(name, value):
        async with tx.pool.admin_conn() as conn, conn.transaction():
            row = await repo.read_global(conn)
            await repo.write_global(conn, expected_revision=row.revision, config={**row.config, name: value})

    async def sharing_value():
        row = await global_read()
        value = row.config.get("sharing", {})
        return {
            "deployment": value.get("deployment", cfg.sharing_viking_deployment),
            "endpoint": value.get("endpoint", cfg.sharing_viking_endpoint),
            "account": value.get("account", cfg.sharing_viking_account),
            "team_user": value.get("team_user", cfg.sharing_viking_user),
            "api_key_present": bool(value.get("secret_ref") or cfg.sharing_viking_api_key),
        }

    @router.get("/api/sharing-config")
    async def sharing(request: Request):
        authorize(request)
        return await sharing_value()

    @router.put("/api/sharing-config")
    async def sharing_save(request: Request):
        authorize(request, True)
        raw = await body(request)
        if set(raw) - {"deployment", "endpoint", "endpoint_override", "account", "team_user",
                       "root_prefix", "api_key", "clear_api_key"}:
            raise HTTPException(400, "unsupported OpenViking setting")
        if "endpoint_override" in raw:
            raw["endpoint"] = raw.pop("endpoint_override")
        row = await global_read()
        value = {**row.config.get("sharing", {}), **{k: raw[k] for k in
                 ("deployment", "endpoint", "account", "team_user") if k in raw}}
        if raw.get("api_key"):
            async with tx.tenant_tx("default") as conn:
                ref = await secret_repository(tx).store(
                    conn, tenant_id="default", plaintext=raw["api_key"], purpose="openviking")
            value["secret_ref"] = ref.ref
        if raw.get("clear_api_key"):
            value.pop("secret_ref", None)
        await global_write("sharing", value)
        return await sharing_value()

    @router.post("/api/sharing-config/bootstrap-dirs", status_code=202)
    async def bootstrap(request: Request):
        authorize(request, True)
        raw = await body(request)
        if set(raw) - {"account"}:
            raise HTTPException(400, "expected account")
        job = "ov-bootstrap:" + uuid.uuid4().hex
        from team_skills.evolution.candidate_pipeline import enqueue_command
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await repo.read_tenant_module(conn, "skills")
            await enqueue_command(conn, services, tenant_id=current_tenant_id(), command_id=job,
                                  kind="skill.evolve", request=raw,
                                  payload={"operation": "ov_bootstrap", "config_revision": row.revision,
                                           "config": row.config, "account": raw.get("account", "")})
        return {"job_id": job, "status_url": f"/api/jobs/{job}"}

    @router.get("/api/team-settings")
    async def team(request: Request):
        authorize(request, True)
        row = await global_read()
        name = row.config.get("display_name", "teamEvolver")
        return {"display_name": name, "configured_display_name": name}

    @router.put("/api/team-settings")
    async def team_save(request: Request):
        authorize(request, True)
        raw = await body(request)
        if set(raw) != {"display_name"} or not isinstance(raw["display_name"], str):
            raise HTTPException(400, "expected display_name")
        await global_write("display_name", raw["display_name"][:200])
        return await team(request)

    @router.get("/api/langfuse-tracing-config")
    async def tracing(request: Request):
        authorize(request, True)
        row = await global_read()
        raw = dict(row.config.get("tracing", {}))
        return {"enabled": False, "host": "", "environment": "local", "release": "",
                "sample_rate": 1, "capture_content": False, **raw,
                "public_key_present": bool(cfg.langfuse_tracing_public_key),
                "secret_key_present": bool(cfg.langfuse_tracing_secret_key)}

    @router.put("/api/langfuse-tracing-config")
    async def tracing_save(request: Request):
        authorize(request, True)
        raw = await body(request)
        allowed = {"enabled", "host", "environment", "release", "sample_rate", "capture_content",
                   "flush_at", "flush_interval_seconds", "public_key", "secret_key"}
        if set(raw) - allowed:
            raise HTTPException(400, "credentials belong to process bootstrap")
        if raw.get("secret_key"):
            async with tx.tenant_tx("default") as conn:
                ref = await secret_repository(tx).store(
                    conn, tenant_id="default", plaintext=raw.pop("secret_key"), purpose="langfuse")
            raw["secret_ref"] = ref.ref
        else:
            raw.pop("secret_key", None)
        await global_write("tracing", raw)
        return await tracing(request)

    @router.post("/api/langfuse-tracing-config/test", status_code=202)
    async def tracing_test(request: Request):
        authorize(request, True)
        raw = await body(request)
        if set(raw) - {"host", "public_key", "secret_key"}:
            raise HTTPException(400, "expected host and tracing credentials")
        if raw.get("secret_key"):
            async with tx.tenant_tx("default") as conn:
                ref = await secret_repository(tx).store(
                    conn, tenant_id="default", plaintext=raw.pop("secret_key"), purpose="langfuse-test")
            raw["secret_ref"] = ref.ref
        job = "tracing-test:" + uuid.uuid4().hex
        from team_skills.evolution.candidate_pipeline import enqueue_command
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await repo.read_tenant_module(conn, "skills")
            await enqueue_command(conn, services, tenant_id=current_tenant_id(), command_id=job,
                                  kind="skill.evolve", request=raw,
                                  payload={"operation": "tracing_test", "config_revision": row.revision,
                                           "config": row.config, "tracing": raw})
        return {"job_id": job, "status_url": f"/api/jobs/{job}"}

    @router.get("/api/storage/status")
    async def storage(request: Request):
        authorize(request)
        return {**tx.pool.pool_status(), "backend": "postgres", "effective_backend": "postgres",
                "session_backend": "postgres", "skill_backend": "postgres", "pg": tx.pool.pool_status()}

    return router
