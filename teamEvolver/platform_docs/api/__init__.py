"""Platform document APIs composed by the application root."""
from fastapi import APIRouter

from .assets import create_router as assets_router
from .console import create_router as console_router
from .workspace import create_router as workspace_router


def create_router(services=None, **kwargs):
    router = APIRouter(tags=["platform-docs"])
    router.include_router(console_router())
    config = (getattr(services, "extras", None) or {}).get("config")
    if config is not None:
        router.include_router(assets_router(config, services))
        router.include_router(workspace_router(config))
    return router
