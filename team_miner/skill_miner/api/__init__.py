"""Skill mining HTTP surface, separate from knowledge Wiki mining."""

from fastapi import APIRouter


def create_router(services=None, **kwargs):
    from .jobs import router as jobs_router
    from .sources import create_router as sources_router

    router = APIRouter(tags=["skill-miner"])
    if services is None or not services.extras.get("config_repo"):
        return router
    router.include_router(jobs_router(services))
    router.include_router(sources_router(services))
    return router
