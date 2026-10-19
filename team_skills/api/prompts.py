"""PG Prompt Studio settings and queued stage tests."""

from __future__ import annotations

import asyncio
import copy

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from session_ingestion.queries import SessionQueries
from teamEvolver.tenants.registry import current_tenant_id

from ..evolution import prompt_studio as studio
from ..evolution.candidate_pipeline import enqueue_command
from ..evolution.prompt_context import prompt_context
from .candidates import accepted, actor, command_id
from .common import read_json


def create_router(services):
    router = APIRouter(tags=["prompt-studio"])
    tx, configs = services.tx, services.extras["config_repo"]

    async def read():
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return await configs.read_tenant_module(conn, "skills")

    async def show(stage_id=None, graph=False):
        row = await read()
        with prompt_context(row.config):
            if graph:
                return studio.pipeline_graph()
            return studio.get_prompt(stage_id) if stage_id else {"prompts": studio.list_prompts()}

    @router.get("/api/skill-evolution/pipeline")
    async def pipeline():
        return await show(graph=True)

    @router.get("/api/skill-evolution/prompts")
    async def prompts():
        return await show()

    @router.get("/api/skill-evolution/prompts/{stage_id}")
    async def detail(stage_id: str):
        try:
            return await show(stage_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown Prompt stage") from exc

    async def update(stage_id, request, reset=False):
        actor(request)
        raw = await read_json(request)
        if stage_id not in studio._STAGE_CATALOG:
            raise HTTPException(404, "unknown Prompt stage")
        if set(raw) - {
            "prompt",
            "model",
            "base_url",
            "temperature",
            "max_tokens",
            "expected_revision",
            "clear_api_key",
            "api_key",
        }:
            raise HTTPException(400, "unsupported Prompt setting")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await configs.read_tenant_module(conn, "skills")
            value = copy.deepcopy(row.config)
            overrides = value.setdefault("prompt_overrides", {})
            settings = value.setdefault("stage_settings", {})
            if reset:
                overrides.pop(stage_id, None)
                settings.pop(stage_id, None)
            else:
                if "prompt" in raw:
                    if not isinstance(raw["prompt"], str) or not raw["prompt"].strip():
                        raise HTTPException(400, "prompt is empty")
                    overrides[stage_id] = raw["prompt"]
                settings[stage_id] = {
                    **settings.get(stage_id, {}),
                    **{k: raw[k] for k in ("model", "base_url", "temperature", "max_tokens") if k in raw},
                }
                if raw.get("clear_api_key"):
                    settings[stage_id].pop("secret_ref", None)
                if raw.get("api_key"):
                    import os

                    from teamEvolver.security import SecretBox, SecretRepository
                    master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get(
                        "TEAMEVOLVER_ROOT_API_KEY")
                    if not master:
                        raise HTTPException(503, "configure secret master key before storing credentials")
                    ref = await SecretRepository(tx, SecretBox(master)).store(
                        conn, tenant_id=current_tenant_id(), plaintext=raw["api_key"], purpose="llm-stage")
                    settings[stage_id]["secret_ref"] = ref.ref
            await configs.write_tenant_module(
                conn, module="skills", expected_revision=raw.get("expected_revision", row.revision), config=value
            )
        with prompt_context(value):
            return studio.get_prompt(stage_id)

    @router.put("/api/skill-evolution/prompts/{stage_id}")
    async def save(stage_id: str, request: Request):
        return await update(stage_id, request)

    @router.post("/api/skill-evolution/prompts/{stage_id}/reset")
    async def reset(stage_id: str, request: Request):
        return await update(stage_id, request, True)

    @router.get("/api/skill-evolution/settings")
    async def process():
        from ..config import DEFAULT_SKILLS_CONFIG
        row = await read()
        evolve_keys = (
            "evolution_max_sessions", "min_group_sessions", "min_group_users",
            "evolution_trigger_mode", "evolution_interval_seconds", "bundle_allow_delete",
            "bundle_prompt_max_bytes", "bundle_edit_max_bytes",
        )
        validation_keys = ("auto_evaluate", "publish_mode")
        return {
            "evolve": {k: row.config.get(k, DEFAULT_SKILLS_CONFIG[k]) for k in evolve_keys},
            "validation": {k: row.config.get(k, DEFAULT_SKILLS_CONFIG[k]) for k in validation_keys},
            "revision": row.revision,
        }

    @router.put("/api/skill-evolution/settings")
    async def process_save(request: Request):
        actor(request)
        raw = await read_json(request)
        if set(raw) - {"evolve", "validation", "revision"}:
            raise HTTPException(400, "expected evolve and validation settings")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await configs.read_tenant_module(conn, "skills")
            evolve_allowed = {
                "evolution_max_sessions", "min_group_sessions", "min_group_users",
                "evolution_trigger_mode", "evolution_interval_seconds", "bundle_allow_delete",
                "bundle_prompt_max_bytes", "bundle_edit_max_bytes",
            }
            validation_allowed = {"auto_evaluate", "publish_mode"}
            evolve = raw.get("evolve", {})
            validation = raw.get("validation", {})
            if (
                not isinstance(evolve, dict) or set(evolve) - evolve_allowed
                or not isinstance(validation, dict) or set(validation) - validation_allowed
            ):
                raise HTTPException(400, "unsupported process parameter")
            if evolve.get("evolution_trigger_mode", "manual") not in {"manual", "automatic"}:
                raise HTTPException(400, "evolution_trigger_mode must be manual or automatic")
            interval = evolve.get("evolution_interval_seconds", 600)
            if type(interval) is not int or not 60 <= interval <= 604800:
                raise HTTPException(400, "evolution_interval_seconds must be 60..604800")
            if validation.get("publish_mode", "manual") not in {"manual", "automatic"}:
                raise HTTPException(400, "publish_mode must be manual or automatic")
            if "auto_evaluate" in validation and type(validation["auto_evaluate"]) is not bool:
                raise HTTPException(400, "auto_evaluate must be boolean")
            await configs.write_tenant_module(
                conn,
                module="skills",
                expected_revision=raw.get("revision", row.revision),
                config={**row.config, **evolve, **validation},
            )
        return await process()

    @router.get("/api/skill-evolution/session-samples")
    async def samples(limit: int = 50):
        async with tx.tenant_tx(current_tenant_id()) as conn:
            rows = await SessionQueries(tx).evolution_page(conn, limit=min(200, max(1, limit)))
        return {"sessions": [{"session_id": r["session_id"], "user_alias": r["user_id"]} for r in rows]}

    @router.post("/api/skill-evolution/prompts/{stage_id}/test", status_code=202)
    async def test(stage_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) - {"session_id", "prompt"} or stage_id not in studio._STAGE_CATALOG:
            raise HTTPException(400, "expected Session and current Prompt stage")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            row = await configs.read_tenant_module(conn, "skills")
            session = await SessionQueries(tx).get_session(conn, session_id=raw.get("session_id", ""))
            if not session:
                raise HTTPException(404, "Session not found")
            snapshot = await asyncio.to_thread(services.extras["session_payloads"].get_session, session["content_ref"])
            command, _ = await enqueue_command(
                conn,
                services,
                tenant_id=current_tenant_id(),
                command_id=command_id(request, "prompt-test"),
                kind="skill.evolve",
                request=raw,
                payload={
                    "operation": "prompt_test",
                    "config_revision": row.revision,
                    "config": row.config,
                    "stage_id": stage_id,
                    "session": snapshot,
                    "prompt": raw.get("prompt"),
                    "actor": reviewer,
                },
            )
        return accepted(command)

    return router
