"""Observability router factory.

Exposes ``create_router`` for the composition root. Hosts the platform health,
readiness, status and logging-status surface migrated out of ``proxy``
(endpoint disposition §1):

  GET /healthz       liveness (process is up)
  GET /readyz        readiness (lifecycle flag; revoked first on drain)
  GET /status        role + readiness summary
  GET /api/logging/status  runtime logging sink state

``/readyz`` reflects the runtime :class:`RuntimeLifecycle` readiness flag so a
load balancer drains a replica before in-flight tasks finish. This
module hosts no business endpoint and holds no business import.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def create_router(services=None, *, lifecycle=None, **kwargs) -> APIRouter:
    """Return the observability ``APIRouter`` for the composition root to mount.

    ``lifecycle`` supplies the readiness flag; ``services`` supplies the role.
    Both are optional so a contract probe / minimal app can mount an honest
    surface (readiness reports ``False`` until a lifecycle is wired).
    """
    router = APIRouter(tags=["operations"])
    role = getattr(services, "role", "api")

    def _ready() -> bool:
        return bool(getattr(lifecycle, "ready", False))

    @router.get("/healthz")
    async def healthz():  # noqa: ANN202
        return {"ok": True, "role": role}

    @router.get("/readyz")
    async def readyz():  # noqa: ANN202
        ready = _ready()
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "role": role})

    @router.get("/status")
    async def status():  # noqa: ANN202
        return {"role": role, "ready": _ready()}

    @router.get("/api/logging/status")
    async def logging_status_route():  # noqa: ANN202
        from ...logging_runtime import logging_status

        return logging_status()

    return router
