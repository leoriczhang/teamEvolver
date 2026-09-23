"""ASGI request diagnostics: no bodies, credentials or query strings are inspected."""

import logging
import re
import time
import uuid

from .logging_runtime import event, log_context

logger = logging.getLogger(__name__)


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = next((v for k, v in scope.get("headers", []) if k.lower() == b"x-request-id"), b"")
        identity = raw.decode("ascii", errors="replace")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identity):
            identity = uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = identity
        status = 500
        response_started = False
        started = time.monotonic()

        async def reply(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message["headers"].append((b"x-request-id", identity.encode("ascii")))
            await send(message)

        with log_context(request_id=identity):
            try:
                await self.app(scope, receive, reply)
            except Exception:
                logger.exception("http.unhandled")
                if response_started:
                    raise
                from starlette.responses import JSONResponse

                await JSONResponse({"detail": "Internal Server Error"}, status_code=500)(scope, receive, reply)
            finally:
                route = getattr(scope.get("route"), "path", None)
                code = "TE_ROUTE_NOT_REGISTERED" if status == 404 and route is None else "HTTP_RESPONSE"
                level = logging.WARNING if status >= 400 else logging.INFO
                if status < 400 and scope["method"] == "GET":
                    level = logging.DEBUG
                event(
                    logger,
                    "http.completed",
                    level,
                    method=scope["method"],
                    route=route or "<unmatched>",
                    status=status,
                    code=code,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    tenant=state.get("tenant_id", ""),
                    user=(state.get("console_user") or {}).get("id", ""),
                )


def install_logging_status(app, admin_guard):
    from fastapi import Request

    from .logging_runtime import logging_status

    @app.get("/api/logging/status", tags=["operations"])
    async def status(request: Request):
        admin_guard(getattr(request.state, "console_user", None))
        return logging_status()
