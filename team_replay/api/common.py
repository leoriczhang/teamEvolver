"""Replay HTTP helpers; bounded bodies, tenant authorization and job responses."""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import Request
from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import TenantConfigMissing

from ..datasets.repository import DatasetError


def actor(request):
    user = getattr(request.state, "console_user", None)
    require_tenant_admin(user)
    return str(user.get("id") or user.get("username") or "")


def root(request):
    if not getattr(request.state, "service_root_authenticated", False):
        raise HTTPException(403, "service Root Key required for Python source access")


async def body(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 96 * 1024 * 1024:
            raise HTTPException(413, "Replay request exceeds 96 MiB")
        raw.extend(chunk)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HTTPException(400, "request must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "request must contain an object")
    return value


def identity(request, prefix):
    key = request.headers.get("idempotency-key", "")
    if len(key) > 200:
        raise HTTPException(400, "Idempotency-Key exceeds 200 characters")
    return prefix + ":" + (hashlib.sha256(key.encode()).hexdigest() if key else uuid.uuid4().hex)


def accepted(record):
    return {"status": "queued", "run_id": record["run_id"], "job_id": record["run_id"],
            "status_url": f"/api/jobs/{record['run_id']}"}


def error(exc):
    return HTTPException(
        503 if isinstance(exc, TenantConfigMissing) else getattr(exc, "status_code", 400), str(exc),
    )


ERRORS = (DatasetError, TenantConfigMissing, ValueError, TypeError)
