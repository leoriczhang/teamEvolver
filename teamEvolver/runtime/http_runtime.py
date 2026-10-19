"""HTTP middleware, public dependencies, exception handling and health endpoints.

Tenant context is bound and reset for every request. Identity and authorization
are injected by the application root. Unknown failures are scrubbed before a
response is returned; business endpoints remain in the owning module APIs."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from ..config_runtime import ConfigRevisionConflict, TenantConfigMissing
from ..tenants.registry import reset_current_tenant, set_current_tenant

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
TRACE_ID_HEADER = "x-trace-id"

# Resolver signature: given the request, return a TenantContext (or None). The
# composition root supplies the real resolver (token/console switch); tests pass
# a fake. Keeping it injected keeps this module free of business logic.
TenantResolver = Callable[[Any], Any]
Authorizer = Callable[[Any, Any], Awaitable[None]]
# Optional response hook the composition root supplies to mutate the outgoing
# response (e.g. renew the console session cookie). Kept injected so this
# platform module carries no business/session knowledge.
ResponseHook = Callable[[Any, Any], None]


def _new_id() -> str:
    return uuid.uuid4().hex


def install_http_runtime(
    app,
    services,
    *,
    tenant_resolver: TenantResolver | None = None,
    authorizer: Authorizer | None = None,
    response_hook: ResponseHook | None = None,
) -> None:
    """Install the common middleware stack onto ``app``.

    ``app`` is a FastAPI/Starlette application. ``tenant_resolver`` resolves the
    request's tenant (bound + reset per request); ``authorizer`` enforces RBAC
    after the tenant is bound. Both are optional so a worker-only process or a
    minimal test app can install the stack without full identity wiring.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # noqa: ANN001
        started = time.monotonic()
        request_id = request.headers.get(REQUEST_ID_HEADER) or _new_id()
        trace_id = request.headers.get(TRACE_ID_HEADER) or request_id
        request.state.request_id = request_id
        request.state.trace_id = trace_id

        tenant_token = None
        try:
            if tenant_resolver is not None:
                tenant_ctx = tenant_resolver(request)
                request.state.tenant = tenant_ctx
            if authorizer is not None:
                await authorizer(request, services)
            # The authorizer may resolve the tenant itself (the console/agent
            # identity path binds ``request.state.tenant``); its result wins over
            # the optional pre-auth resolver. Bind the contextvar once identity is
            # settled so repositories and handlers see the request's tenant.
            tenant_ctx = getattr(request.state, "tenant", None)
            if tenant_ctx is not None:
                tenant_token = set_current_tenant(tenant_ctx)
            response = await call_next(request)
        except _HTTP_EXCEPTIONS as exc:  # mapped domain/HTTP errors
            response = JSONResponse(
                status_code=getattr(exc, "status_code", 400),
                content={"detail": getattr(exc, "detail", str(exc)), "request_id": request_id},
            )
        except (ValueError, TypeError, KeyError) as exc:
            response = JSONResponse(status_code=400 if not isinstance(exc, KeyError) else 404,
                                    content={"detail": "invalid_request", "request_id": request_id})
        except (ConfigRevisionConflict, TenantConfigMissing) as exc:
            response = JSONResponse(status_code=409 if isinstance(exc, ConfigRevisionConflict) else 503,
                                    content={"detail": str(exc), "request_id": request_id})
        except Exception:  # noqa: BLE001 - unexpected errors become a scrubbed 500
            logger.exception("http.unhandled_error", extra={"request_id": request_id, "trace_id": trace_id})
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal_error", "request_id": request_id},
            )
        finally:
            if tenant_token is not None:
                reset_current_tenant(tenant_token)

        if response_hook is not None:
            try:
                response_hook(request, response)
            except Exception:  # noqa: BLE001 - a hook failure must not break the response
                logger.warning("http.response_hook_failed", extra={"request_id": request_id})

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.info(
            "http.access",
            extra={
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        return response


try:  # Resolve the HTTPException type once, lazily, without a hard import at module load.
    from fastapi import HTTPException as _FastAPIHTTPException
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    _HTTP_EXCEPTIONS: tuple[type[BaseException], ...] = (_FastAPIHTTPException, _StarletteHTTPException)
except Exception:  # pragma: no cover - fastapi always present where middleware runs
    _HTTP_EXCEPTIONS = ()


def build_health_app(lifecycle, *, role: str):
    """A minimal FastAPI app exposing ``/healthz`` and ``/readyz`` for a role.

    ``/healthz`` is liveness (the process is up); ``/readyz`` reflects the
    lifecycle readiness flag, which is revoked first on graceful shutdown so a
    load balancer drains the replica before in-flight tasks finish.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title=f"teamEvolver-{role}", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz():  # noqa: ANN202
        return {"ok": True, "role": role}

    @app.get("/readyz")
    async def readyz():  # noqa: ANN202
        ready = lifecycle.ready
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "role": role})

    return app
