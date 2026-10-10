"""Memory maintenance, historical changes and replay routes."""

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


def register_replay_routes(owner, app: FastAPI, *, session_user):
    from teamEvolver.proxy.routes import _require_admin_user, _tenant_effective_config

    _session_user = session_user

    @app.post("/api/memory/maintenance/run")
    async def trigger_memory_maintenance(request: Request):
        _require_admin_user(
            getattr(request.state, "console_user", None)
            or _session_user(request)
        )
        result = owner._trigger_memory_maintenance()
        status = str(result.get("status") or "")
        if status == "not_configured":
            return JSONResponse(content=result, status_code=503)
        return JSONResponse(content=result, status_code=202)

    @app.get("/api/memory/maintenance/status")
    async def memory_maintenance_status(request: Request):
        _require_admin_user(
            getattr(request.state, "console_user", None)
            or _session_user(request)
        )
        return owner._memory_maintenance_status()

    @app.get("/api/memory/maintenance/dry-run")
    async def memory_maintenance_dry_run(request: Request):
        _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
        return owner._memory_maintenance_dry_run()

    @app.get("/api/memory/changes")
    async def memory_changes(
        request: Request,
        limit: int = 100,
    ):
        _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=400,
                detail="limit must be between 1 and 500",
            )
        return owner._memory_changes(
            limit=limit,
            config=_tenant_effective_config(owner),
        )

    @app.post("/api/memory/changes/{change_id}/true-replay")
    async def memory_true_replay(
        change_id: str,
        request: Request,
    ):
        _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Memory True Replay body must be an object",
            )
        raw_checklist = body.get("checklist")
        if isinstance(raw_checklist, str):
            checklist = [line.strip() for line in raw_checklist.splitlines() if line.strip()]
        elif isinstance(raw_checklist, list):
            checklist = list(raw_checklist)
        else:
            checklist = []
        try:
            return await asyncio.to_thread(
                owner._run_memory_replay,
                change_id=change_id,
                query=str(body.get("query") or ""),
                checklist=checklist,
                source_session_id=str(body.get("source_session_id") or ""),
                max_interactions=int(body.get("max_interactions") or 4),
                timeout_seconds=int(body.get("timeout_seconds") or 600),
                config=_tenant_effective_config(owner),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

    @app.get("/api/memory/changes/{change_id}/true-replays")
    async def memory_true_replays(
        change_id: str,
        request: Request,
        limit: int = 100,
    ):
        _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=400,
                detail="limit must be between 1 and 500",
            )
        return owner._memory_replays(
            change_id=change_id,
            limit=limit,
            config=_tenant_effective_config(owner),
        )

    @app.post("/api/openviking/memory/true-replay")
    async def memory_adhoc_true_replay(request: Request):
        """A/B replay an unsaved Memory edit: stored version vs draft.

        Backs the workspace "Memory 真回放" panel — Baseline uses the
        stored file content, Candidate uses the workspace draft, both
        share the rest of a Source Session's context.
        """
        _session_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Memory replay body must be an object",
            )
        raw_checklist = body.get("checklist")
        if isinstance(raw_checklist, str):
            checklist = [line.strip() for line in raw_checklist.splitlines() if line.strip()]
        elif isinstance(raw_checklist, list):
            checklist = list(raw_checklist)
        else:
            checklist = []
        try:
            return await asyncio.to_thread(
                owner._run_memory_replay_adhoc,
                memory_path=str(body.get("memory_path") or ""),
                before_content=str(body.get("before_content") or ""),
                after_content=str(body.get("after_content") or ""),
                query=str(body.get("query") or ""),
                checklist=checklist,
                scope=str(body.get("scope") or "team_memory"),
                source_session_id=str(body.get("source_session_id") or ""),
                max_interactions=int(body.get("max_interactions") or 4),
                timeout_seconds=int(body.get("timeout_seconds") or 600),
                config=_tenant_effective_config(owner),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc
