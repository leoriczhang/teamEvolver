"""Composition entry point for the two independent mining businesses."""

from fastapi import APIRouter


def create_router(services=None, **kwargs):
    from team_miner.knowledge_miner.api import create_router as knowledge_router
    from team_miner.skill_miner.api import create_router as skill_router

    router = APIRouter()
    router.include_router(knowledge_router(services=services, **kwargs))
    router.include_router(skill_router(services=services, **kwargs))
    return router
