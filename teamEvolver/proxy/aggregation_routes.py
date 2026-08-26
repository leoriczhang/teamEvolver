"""HTTP routes for cross-user memory aggregation (interface 1).

Endpoints (admin-only; the pipeline uses a trusted/root service identity):

- ``POST /api/aggregation/run``       -> start a background aggregation task
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..aggregation import MemoryAggregationService
from .users_admin import _request_user

logger = logging.getLogger(__name__)


class AggregationMixin:
    """Team-memory aggregation console endpoints."""

    def _aggregation_service(self) -> MemoryAggregationService:
        service = getattr(self, "_aggregation_service_instance", None)
        if service is None or getattr(service, "config", None) is not self.config:
            service = MemoryAggregationService(self.config)
            self._aggregation_service_instance = service
        return service

    @staticmethod
    def _require_admin(request: Request) -> None:
        user = _request_user(request)
        if str(user.get("role") or "user") != "admin":
            raise HTTPException(
                status_code=403,
                detail="team memory aggregation requires an administrator",
            )

    def _register_aggregation_routes(self, app: FastAPI) -> None:

        @app.get("/api/aggregation/users")
        async def api_aggregation_users(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            account_id = str(
                request.query_params.get("account_id")
                or getattr(self.config, "sharing_viking_account", "")
                or "default"
            ).strip()
            if not account_id:
                raise HTTPException(status_code=400, detail="account_id is required")
            service = self._aggregation_service()
            try:
                users = await service.list_account_users(account_id)
            except Exception as exc:  # noqa: BLE001 - surface as 400 for the console
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse({"account_id": account_id, "users": users})

        @app.get("/api/aggregation/runs")
        async def api_aggregation_runs(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            return JSONResponse({"runs": self._aggregation_service().list_runs()})

        @app.post("/api/aggregation/run", status_code=202)
        async def api_aggregation_run(request: Request):
            self._mark_request_activity()
            self._require_admin(request)
            try:
                body = await request.json()
            except ValueError:
                body = {}
            account_id = str(
                (body or {}).get("account_id")
                or getattr(self.config, "sharing_viking_account", "")
                or "default"
            ).strip()
            if not account_id:
                raise HTTPException(status_code=400, detail="account_id is required")
            kinds = (body or {}).get("kinds")
            kinds = [str(k) for k in kinds] if isinstance(kinds, list) else None
            user_ids = (body or {}).get("user_ids")
            user_ids = (
                [str(u) for u in user_ids] if isinstance(user_ids, list) else None
            )
            full = str((body or {}).get("mode") or "").lower() == "full"

            service = self._aggregation_service()
            run = service.new_run(account_id)

            def _worker() -> None:
                service.run(run, kinds=kinds, full=full, user_ids=user_ids)

            threading.Thread(target=_worker, daemon=True).start()
            return JSONResponse(status_code=202, content=run.to_public())

        @app.get("/api/aggregation/status/{task_id}")
        async def api_aggregation_status(task_id: str, request: Request):
            self._mark_request_activity()
            self._require_admin(request)
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
