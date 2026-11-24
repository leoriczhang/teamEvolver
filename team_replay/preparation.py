"""One fenced, immutable preparation checkpoint shared by every Replay entry."""

from .repository import ReplayRepository


async def prepare_cases(task, services, cases, skill_name="", records=None):
    hook = services.extras.get("ma_prepare_replay")
    if not hook:
        return None
    repo = ReplayRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        saved = await repo.preparation(conn, task.tenant_id, task.task_id, task.payload_hash)
    if saved is not None:
        return saved
    result = await hook(task, cases, skill_name, records=records or {})
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        return await repo.save_preparation(
            conn, tenant_id=task.tenant_id, run_id=task.task_id, input_hash=task.payload_hash, result=result,
        )
