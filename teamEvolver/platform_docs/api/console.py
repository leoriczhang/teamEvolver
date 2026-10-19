"""Built SPA entrypoints, separate from business routes."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException


def create_router():
    router = APIRouter(tags=["console"], include_in_schema=False)
    entry = Path(__file__).resolve().parents[2] / "web/dist/index.html"

    @router.get("/")
    @router.get("/console")
    async def console():
        if not entry.is_file():
            raise HTTPException(503, "console assets are not built")
        return FileResponse(entry)
    return router
