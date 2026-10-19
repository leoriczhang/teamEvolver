"""Replay Router factory for the single application composition root."""

from __future__ import annotations

from fastapi import APIRouter


def create_router(services=None, **kwargs) -> APIRouter:
    """Return this module's ``APIRouter`` for the composition root to mount."""
    router = APIRouter()
    if services is None or not services.extras.get("config_repo"):
        return router
    from .adapters import create_router as adapters
    from .datasets import create_router as datasets
    from .runs import create_router as runs

    router.include_router(datasets(services))
    router.include_router(adapters(services))
    router.include_router(runs(services))
    return router
