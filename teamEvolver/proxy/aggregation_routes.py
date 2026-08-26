"""HTTP routes for cross-user memory aggregation (interface 1).

The reusable execution endpoints do not depend on TeamEvolver sessions or
roles. OpenViking access uses one request-scoped Root/Admin credential.
Management endpoints remain console-admin-only.

- ``POST /api/aggregation/run``       -> start a background aggregation task
- ``POST /api/aggregation/users``     -> list users with an OpenViking credential
- ``GET  /api/aggregation/status/{id}`` -> poll a task's per-category progress
- ``GET  /api/aggregation/okf-skill`` -> read the default OKF Skill body
- ``PUT  /api/aggregation/okf-skill`` -> (placeholder) persist an edited Skill

The mixin owns a single :class:`MemoryAggregationService` bound to the current
config and runs each task in a worker thread (same style as DreamCycle's
trigger), so the request returns 202 immediately.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..aggregation import MemoryAggregationService
from ..config_store import ConfigStore
from .users_admin import _request_user

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AggregationRequestContext:
    endpoint: str
    account_id: str
    api_key: str
    auth_mode: str


class AggregationMixin:
    """Team-memory aggregation console endpoints."""

    def _aggregation_service(self) -> MemoryAggregationService:
        service = getattr(self, "_aggregation_service_instance", None)
        if service is None or getattr(service, "config", None) is not self.config:
            service = MemoryAggregationService(self.config)
            self._aggregation_service_instance = service
        return service

    def _aggregation_request_context(
        self,
        request: Request,
        body: dict,
    ) -> _AggregationRequestContext:
        account_id = str(
            body.get("account_id")
            or getattr(self.config, "sharing_viking_account", "")
            or "default"
        ).strip()
        raw_endpoint = body.get("endpoint")
        if raw_endpoint is not None and not isinstance(raw_endpoint, str):
            raise HTTPException(status_code=400, detail="endpoint must be a string")
        try:
            endpoint = self._aggregation_service().resolve_endpoint(raw_endpoint)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw_root_key = body.get("root_key")
        raw_admin_key = body.get("admin_key")
        if raw_root_key is not None and not isinstance(raw_root_key, str):
            raise HTTPException(status_code=400, detail="root_key must be a string")
        if raw_admin_key is not None and not isinstance(raw_admin_key, str):
            raise HTTPException(status_code=400, detail="admin_key must be a string")
        root_key = str(raw_root_key or "").strip()
        admin_key = str(raw_admin_key or "").strip()
        if root_key and admin_key:
            raise HTTPException(
                status_code=400,
                detail="root_key and admin_key are mutually exclusive",
            )

        if root_key:
            api_key = root_key
            auth_mode = "trusted"
        elif admin_key:
            api_key = admin_key
            auth_mode = "api_key"
        else:
            user = _request_user(request)
            if str(user.get("role") or "") != "admin":
                raise HTTPException(
                    status_code=400,
                    detail="exactly one of root_key or admin_key is required",
                )
            api_key = str(
                getattr(self.config, "sharing_viking_team_api_key", "")
                or getattr(self.config, "sharing_viking_api_key", "")
                or ""
            ).strip()
            if not api_key:
                raise HTTPException(
                    status_code=400,
                    detail="trusted root key is not configured",
                )
            auth_mode = "trusted"

        return _AggregationRequestContext(
            endpoint=endpoint,
            account_id=account_id,
            api_key=api_key,
            auth_mode=auth_mode,
        )

    @staticmethod
    def _require_admin(request: Request) -> None:
        user = _request_user(request)
        if str(user.get("role") or "user") != "admin":
            raise HTTPException(
                status_code=403,
                detail="team memory aggregation requires an administrator",
            )

    @staticmethod
    def _aggregation_settings_payload(agg: dict) -> dict:
        prefix = str(agg.get("shared_knowledge_prefix") or "shared-knowledge")
        staging = str(agg.get("staging_dir") or "staging")
        clean_prefix = prefix.strip("/")
        clean_staging = staging.strip("/")
        return {
            "enabled": bool(agg.get("enabled", False)),
            "shared_knowledge_prefix": prefix,
            "target_root": f"viking://resources/{clean_prefix}",
            "staging_dir": staging,
            "work_root": f"viking://resources/{clean_prefix}-{clean_staging}",
            "okf_skill_uri": str(
                agg.get("okf_skill_uri") or "viking://agent/skills/team-memory-okf"
            ),
            "key_seed": str(agg.get("key_seed") or "teamevolver-aggregation"),
            "kinds": agg.get("kinds") or [],
        }

    def _register_aggregation_routes(self, app: FastAPI) -> None:

        @app.post("/api/aggregation/users")
        async def api_aggregation_users(request: Request):
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="aggregation users body must be an object",
                )
            context = self._aggregation_request_context(request, body)
            service = self._aggregation_service()
            try:
                users = await service.list_account_users(
                    context.account_id,
                    api_key=context.api_key,
                    endpoint=context.endpoint,
                )
            except Exception as exc:  # noqa: BLE001 - surface as 400 for the console
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(
                {
                    "endpoint": context.endpoint,
                    "account_id": context.account_id,
                    "auth_mode": context.auth_mode,
                    "users": users,
                }
            )

        @app.get("/api/aggregation/runs")
        async def api_aggregation_runs(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            return JSONResponse({"runs": self._aggregation_service().list_runs()})

        @app.post("/api/aggregation/run", status_code=202)
        async def api_aggregation_run(request: Request):
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="aggregation run body must be an object",
                )
            context = self._aggregation_request_context(request, body)
            kinds = body.get("kinds")
            kinds = [str(k) for k in kinds] if isinstance(kinds, list) else None
            user_ids = body.get("user_ids")
            user_ids = (
                [str(u) for u in user_ids] if isinstance(user_ids, list) else None
            )
            full = str(body.get("mode") or "").lower() == "full"
            target_uri = body.get("target_uri")
            if target_uri is not None and not isinstance(target_uri, str):
                raise HTTPException(
                    status_code=400,
                    detail="target_uri must be a string",
                )

            service = self._aggregation_service()
            try:
                run = service.new_run(
                    context.account_id,
                    endpoint=context.endpoint,
                    auth_mode=context.auth_mode,
                    target_uri=target_uri,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            def _worker() -> None:
                service.run(
                    run,
                    kinds=kinds,
                    full=full,
                    user_ids=user_ids,
                    api_key=context.api_key,
                )

            threading.Thread(target=_worker, daemon=True).start()
            return JSONResponse(status_code=202, content=run.to_public())

        @app.get("/api/aggregation/status/{task_id}")
        async def api_aggregation_status(task_id: str):
            status = self._aggregation_service().status(task_id)
            if status is None:
                raise HTTPException(status_code=404, detail="unknown aggregation task")
            return JSONResponse(status)

        @app.get("/api/aggregation/okf-skill")
        async def api_aggregation_okf_skill(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            service = self._aggregation_service()
            return JSONResponse(
                {
                    "skill_name": service._skill_name(),
                    "body": service.skill_body(),
                    "editable": True,
                }
            )

        @app.put("/api/aggregation/okf-skill")
        async def api_aggregation_okf_skill_save(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            try:
                payload = await request.json()
            except ValueError:
                payload = {}
            body = str((payload or {}).get("body") or "")
            service = self._aggregation_service()
            try:
                service.save_skill_body(body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # The saved body is installed into each identity's own skills space on
            # the next aggregation run, so no separate publish step is needed here.
            return JSONResponse({"ok": True, "body": service.skill_body()})

        @app.get("/api/aggregation/settings")
        async def api_aggregation_settings(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            config_file = str(
                getattr(self.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=__import__("pathlib").Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            agg = data.get("aggregation", {}) if isinstance(data.get("aggregation"), dict) else {}
            return JSONResponse(self._aggregation_settings_payload(agg))

        @app.post("/api/aggregation/settings")
        async def api_aggregation_settings_save(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="aggregation settings body must be an object")

            config_file = str(
                getattr(self.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=__import__("pathlib").Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            agg = data.setdefault("aggregation", {})

            if "shared_knowledge_prefix" in body:
                prefix = str(body.get("shared_knowledge_prefix") or "").strip().strip("/")
                if not prefix:
                    raise HTTPException(status_code=400, detail="shared_knowledge_prefix is required")
                if len(prefix) > 120:
                    raise HTTPException(
                        status_code=400,
                        detail="shared_knowledge_prefix must be at most 120 characters",
                    )
                agg["shared_knowledge_prefix"] = prefix

            if "staging_dir" in body:
                agg["staging_dir"] = str(body.get("staging_dir") or "staging").strip().strip("/")

            if "okf_skill_uri" in body:
                agg["okf_skill_uri"] = str(body.get("okf_skill_uri") or "").strip()

            if "kinds" in body:
                raw_kinds = body.get("kinds")
                if isinstance(raw_kinds, list):
                    agg["kinds"] = [str(k).strip() for k in raw_kinds if str(k).strip()]

            store.save(data)
            new_config = store.to_config()
            self.config = new_config
            await self._reload_openviking_integrations(new_config)

            payload = self._aggregation_settings_payload(agg)
            payload["ok"] = True
            return JSONResponse(payload)
