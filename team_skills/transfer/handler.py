"""Fenced transfer Worker; network/ZIP/Git work stays outside API transactions."""
import asyncio
import threading

from team_skills.evolution.repository import digest
from team_skills.library.repository import SkillLibraryError
from teamEvolver.runtime import HandlerResult

from .commands import TransferCommands
from .packages import TransferError
from .service import SkillTransferService


async def transfer(task, services):
    commands = TransferCommands(services)
    service = SkillTransferService(services.tx, services.extras.get("skill_transfer_adapters"))
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        command = await commands.get(conn, task.payload_ref)
        if (not command or command["payload_hash"] != task.payload_hash
                or digest(command["payload"]) != task.payload_hash):
            return HandlerResult.fail("transfer_payload_mismatch")
        if command["result"] is not None:
            return HandlerResult.succeed(task.task_id)
        payload = command["payload"]
        options = dict(payload["options"])
        ref = options.pop("secret_ref", "")
        if ref:
            try:
                options["token"] = await services.extras["secret_repo"].resolve(conn, secret_ref=ref)
            except (KeyError, ValueError):
                return HandlerResult.fail("transfer_secret_unavailable")
    loop, stopped = asyncio.get_running_loop(), threading.Event()
    checkpoint = command["checkpoint"]

    async def guard(update=None):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            if update is not None:
                await commands.checkpoint(conn, task.task_id, update)

    def check(update=None):
        if stopped.is_set():
            raise TransferError("任务租约已失效", 409)
        future = asyncio.run_coroutine_threadsafe(guard(update), loop)
        try:
            future.result(timeout=15)
        except Exception:
            future.cancel()
            stopped.set()
            raise TransferError("任务租约已失效", 409) from None
        if update is not None:
            checkpoint.clear()
            checkpoint.update(update)

    options["_check"] = check
    options["_checkpoint"] = check
    adapter = service.adapter(payload["channel"])
    try:
        if command["operation"] == "import":
            packages = await asyncio.to_thread(adapter.read, options)
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                result = await service.import_skills(
                    conn, tenant_id=task.tenant_id, command_id=task.task_id, payload=payload, packages=packages,
                )
                await commands.finish(conn, task.task_id, result)
        else:
            if checkpoint:
                # An interrupted effect is never automatically sent again.
                receipt = checkpoint.get("receipt")
                if receipt is None and hasattr(adapter, "reconcile"):
                    receipt = await asyncio.to_thread(adapter.reconcile, checkpoint, options)
                if receipt is None:
                    return HandlerResult.reconcile()
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    await services.tasks.require_current(conn, task)
                    await commands.finish(conn, task.task_id, receipt)
                return HandlerResult.succeed(task.task_id)
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                packages = await service.export_packages(conn, payload)
            result = await asyncio.to_thread(adapter.write, packages, options)
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await commands.finish(conn, task.task_id, result.metadata, result.content)
        return HandlerResult.succeed(task.task_id)
    except (TransferError, SkillLibraryError) as exc:
        if checkpoint:
            return HandlerResult.reconcile()
        code = f"transfer_error_{exc.status_code}"
        return HandlerResult.retry(code) if exc.status_code in {408, 429, 502, 503, 504} else HandlerResult.fail(code)
    except Exception:
        # No raw transport exception (including nested credentials) reaches logs.
        return HandlerResult.reconcile() if checkpoint else HandlerResult.retry("transfer_failed")
    finally:
        stopped.set()
