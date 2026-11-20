"""Resolve cloud snapshots before entering endpoint transactions."""
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.routing import request_response

from team_skills.library.repository import SkillLibraryError
from team_skills.library.store import skill_scope
from teamEvolver.managed_agents import MAError
from teamEvolver.tenants.registry import current_tenant_id


def install(router, services):
    def wrap(endpoint):
        async def call(request):
            params = request.path_params
            path = request.url.path
            # Dataset edits and historical reads use PG snapshots only. A
            # cloud outage must not block inspecting an existing experiment.
            if path.startswith("/api/replay-lab/") and (
                (request.method == "GET" and ("/datasets" in path or "/runs" in path or "/jobs/" in path))
                or ("/datasets" in path and not path.endswith("/synthesize"))
            ):
                return await endpoint(request)
            name, version = params.get("name"), params.get("version")
            versions = {name: [int(version)] if version is not None else []} if (
                name and "/versions" in request.url.path) else None
            try:
                # Pin Dataset revisions before the acceptance transaction,
                # including historical versions never viewed in the console.
                if request.method == "POST" and request.url.path.endswith("/runs"):
                    dataset_id = params.get("dataset_id")
                    if not dataset_id:
                        raw = await request.json()
                        dataset_id = raw.get("dataset_id")
                    if dataset_id:
                        from team_replay.datasets.repository import DatasetRepository
                        async with services.tx.tenant_tx(current_tenant_id()) as conn:
                            fixed = await DatasetRepository(services.tx).freeze(conn, dataset_id)
                        versions = {ref["skill_id"]: [ref["revision"]] if ref.get("revision") else []
                                    for ref in fixed["document"]["skills"]}
                selected = (request.query_params.get("connection_id", "")
                            or request.headers.get("x-ma-connection-id", ""))
                async with skill_scope(services, current_tenant_id(), versions=versions, connection_id=selected):
                    response = await endpoint(request)
                import json
                if response.status_code == 200 and response.media_type == "application/json":
                    value = json.loads(response.body)
                    if isinstance(value, dict) and value.get("status") == "accepted" and value.get("job_id"):
                        response.status_code = 202
                return response
            except SkillLibraryError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            except MAError as exc:
                raise HTTPException(503, str(exc)) from exc
        return call

    class ScopedRoute(APIRoute):
        # include_router reconstructs routes. Preserve the scope through the
        # route type, rather than attaching only to the discarded route.app.
        def get_route_handler(self):
            return wrap(super().get_route_handler())

    visited = set()

    def install_routes(current):
        if id(current) in visited:
            return
        visited.add(id(current))
        for route in current.routes:
            if isinstance(route, APIRoute) and not isinstance(route, ScopedRoute):
                route.__class__ = ScopedRoute
                route.app = request_response(route.get_route_handler())
            # FastAPI 0.143 keeps included routers as references rather than
            # flattening their routes. Scope the original routes before their
            # effective route tables are compiled.
            included = getattr(route, "original_router", None)
            if included is not None:
                install_routes(included)
        invalidate = getattr(current, "_mark_routes_changed", None)
        if invalidate is not None:
            invalidate()

    install_routes(router)
