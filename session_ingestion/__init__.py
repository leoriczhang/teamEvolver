"""Session ingestion from Agent pushes and tenant-owned upstream sources."""

from __future__ import annotations

from typing import Any, Callable


async def ingest(
    owner: Any,
    session: dict[str, Any],
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Compatibility-friendly facade over the shared ingestion module."""
    from .service import ingest as ingest_session

    return await ingest_session(
        owner,
        session,
        invalidate_cache=invalidate_cache,
    )


def register_routes(
    owner: Any,
    app: Any,
    *,
    invalidate_cache: Callable[..., None] | None = None,
) -> None:
    """Register every Session ingestion transport on one shared interface."""
    from .pull.routes import register_pull_routes
    from .push.routes import register_push_routes

    register_push_routes(owner, app, invalidate_cache=invalidate_cache)
    register_pull_routes(owner, app, invalidate_cache=invalidate_cache)


__all__ = [
    "ingest",
    "register_routes",
]
