"""MA long I/O executes only on fenced Worker attempts."""

import asyncio

from teamEvolver.managed_agents import MAClient, MAError
from teamEvolver.runtime import HandlerResult

from .collection import collect_connection
from .discovery import discover_connection
from .repository import MARepository
from .schemas import digest


async def handle(task, services):
    key = "ma-connection:" + task.subject_id
    owner = f"{task.owner_id}:{task.task_id}:{task.fence}"
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        fence = await services.leases.acquire(conn, tenant_id=task.tenant_id, resource_key=key,
                                              owner_id=owner, lease_seconds=60)
    if fence is None:
        return HandlerResult.retry("ma_connection_busy", backoff_seconds=5)
    work = asyncio.create_task(_handle(task, services))
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=15)
            if work.done():
                break
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                if not await services.leases.heartbeat(
                    conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, fence=fence, lease_seconds=60,
                ):
                    raise RuntimeError("MA connection lease lost")
        return work.result()
    except Exception:
        await _error(MARepository(services.tx), services, task, "ma_operation_failed")
        return HandlerResult.retry("ma_operation_failed")
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.leases.release(conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, fence=fence)


async def _handle(task, services):
    repo = MARepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        operation = await repo.operation(conn, task.payload_ref)
        if (not operation or operation["input_hash"] != task.payload_hash
                or digest(operation["input"]) != task.payload_hash):
            return HandlerResult.fail("ma_payload_mismatch")
        if operation["result"] is not None:
            return HandlerResult.succeed(task.task_id)
        connection = await repo.connection(conn, task.subject_id, operation["input"]["revision"])
        if not connection or connection["config_hash"] != operation["input"]["config_hash"]:
            return HandlerResult.fail("ma_config_snapshot_missing")
        if not connection["enabled"]:
            return HandlerResult.cancelled()
        if task.kind != "ma.discover" and connection["status"] not in {"ready", "syncing", "error", "drift"}:
            return HandlerResult.fail("ma_discovery_required")
        token = await services.extras["secret_repo"].resolve(conn, secret_ref=connection["config"]["ma_secret_ref"])
    factory = services.extras.get("ma_client_factory") or MAClient
    async with factory(connection["config"]["ma_base_url"], token) as client:
        try:
            if task.kind == "ma.discover":
                result = await discover_connection(task, services, client, connection, operation)
                if result.get("waiting"):
                    # Imports are independent Worker tasks; waiting is not a failed attempt.
                    return HandlerResult.wait_external("ma-import:" + task.task_id)
            elif task.kind == "ma.collect":
                await collect_connection(task, services, client, connection, operation)
            else:
                from .synchronization import reconcile_connection
                await reconcile_connection(task, services, client, connection, operation)
            return HandlerResult.succeed(task.task_id)
        except MAError as exc:
            code = "ma_" + exc.code[:100]
            await _error(repo, services, task, code)
            if exc.code == "TransportOutcomeUnknown":
                return HandlerResult.reconcile()
            if exc.status in {408, 429, 500, 502, 503, 504}:
                return HandlerResult.retry(code)
            return HandlerResult.fail(code)
        except ValueError as exc:
            # Validation errors are authored here and contain no upstream bodies.
            await _error(repo, services, task, str(exc)[:400])
            return HandlerResult.fail("ma_validation_failed")
        except Exception:
            # Do not propagate transport URLs/credentials through Worker logs.
            await _error(repo, services, task, "ma_operation_failed")
            return HandlerResult.retry("ma_operation_failed")


async def _error(repo, services, task, code):
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.status(conn, task.subject_id, "error", {"error": code, "job_id": task.task_id})


def register_handlers():
    return {kind: handle for kind in ("ma.discover", "ma.collect", "ma.reconcile")}
