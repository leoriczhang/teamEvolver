"""Short transaction acceptance of immutable MA Worker operations."""

from uuid import uuid4

from .repository import MARepository
from .schemas import digest


async def enqueue(conn, services, *, tenant_id, connection_id, kind, body=None, operation_id=None):
    repo = MARepository(services.tx)
    connection = await repo.connection(conn, connection_id)
    if connection is None:
        raise KeyError("MA connection not found")
    if kind not in {"ma.discover", "ma.collect", "ma.reconcile"}:
        raise ValueError("Unknown MA operation")
    operation_id = operation_id or "ma:" + uuid4().hex
    # Idempotent repeats return the already fixed revision, even after config edits.
    existing = await repo.operation(conn, operation_id)
    payload = {"connection_id": connection_id, "revision": connection["revision"],
               "config_hash": connection["config_hash"], "request": body or {}}
    if existing:
        if existing["connection_id"] != connection_id or existing["kind"] != kind:
            raise ValueError("Idempotency-Key belongs to another operation")
        payload.update({k: existing["input"][k] for k in ("revision", "config_hash")})
    operation, created = await repo.accept(
        conn, tenant_id=tenant_id, connection_id=connection_id, operation_id=operation_id, kind=kind, payload=payload,
    )
    if created:
        await services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=operation_id, kind=kind, subject_type="ma_connection",
            subject_id=connection_id, schema_version="ma-operation.v1", dedupe_key=operation_id,
            payload_ref=operation_id, payload_hash=digest(payload), config_revision=connection["revision"],
        )
    return {"accepted": True, "job_id": operation["operation_id"], "status": "accepted",
            "status_url": f"/api/jobs/{operation['operation_id']}", "dedupe": not created}
