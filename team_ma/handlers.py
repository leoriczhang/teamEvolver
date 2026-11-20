"""MA long I/O executes only on fenced Worker attempts."""

import asyncio

from teamEvolver.managed_agents import MAClient, MAError
from teamEvolver.runtime import HandlerResult

from .collection import collect_connection
from .discovery import discover_connection
from .effects import UnknownEffect
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
        return HandlerResult.defer("ma_connection_busy", backoff_seconds=5)
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
            elif task.kind == "ma.skill":
                from .skill_writes import write_skill
                await write_skill(task, services, client, connection, operation)
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    await services.tasks.require_current(conn, task)
                    await repo.operation_status(conn, task.subject_id, task.kind)
            elif task.kind == "ma.collect":
                await collect_connection(task, services, client, connection, operation)
            else:
                from .synchronization import reconcile_connection
                await reconcile_connection(task, services, client, connection, operation)
            return HandlerResult.succeed(task.task_id)
        except MAError as exc:
            code = "ma_" + exc.code[:100]
            retryable = exc.status in {408, 429, 500, 502, 503, 504} or exc.code == "TransportOutcomeUnknown"
            await _error(repo, services, task, code, retryable=retryable)
            if exc.code == "TransportOutcomeUnknown":
                return HandlerResult.reconcile()
            if retryable:
                return HandlerResult.retry(code)
            return HandlerResult.fail(code)
        except UnknownEffect:
            await _error(repo, services, task, "ma_outcome_unknown")
            return HandlerResult.reconcile()
        except ValueError as exc:
            # Validation errors are authored here and contain no upstream bodies.
            await _error(repo, services, task, str(exc)[:400], retryable=False)
            return HandlerResult.fail("ma_validation_failed")
        except Exception:
            # Do not propagate transport URLs/credentials through Worker logs.
            await _error(repo, services, task, "ma_operation_failed")
            return HandlerResult.retry("ma_operation_failed")


async def _error(repo, services, task, code, *, retryable=True):
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.operation_status(conn, task.subject_id, task.kind,
                                    error=code, job_id=task.task_id, retryable=retryable)
        if task.kind == "ma.skill" and not retryable:
            callback = services.extras.get("cloud_skill_failed")
            operation = await repo.operation(conn, task.task_id)
            if callback and operation:
                await callback(conn, task, operation["input"]["request"], code)


def register_handlers():
    return {kind: handle for kind in ("ma.discover", "ma.collect", "ma.reconcile", "ma.skill")}
