"""Skill storage seam: remote I/O precedes the caller's short transaction.

The composition root injects a provider. A request/Worker scope resolves cloud
heads and immutable bytes before repositories execute. Repository reads then
consume that fixed snapshot; remote writes are durable Worker commands.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar

_snapshot = ContextVar("skill_store_snapshot", default=None)


def snapshot(tx):
    value = _snapshot.get()
    return value if value is not None and value.provider.services.tx is tx else None


@asynccontextmanager
async def skill_scope(services, tenant_id, *, versions=None, connection_id=""):
    provider = services.extras.get("cloud_skill_provider")
    if provider is None:
        yield None
        return
    existing = snapshot(services.tx)
    if existing is not None and existing.tenant == tenant_id:
        yield existing
        return
    value = await provider.load(tenant_id, versions=versions, connection_id=connection_id)
    token = _snapshot.set(value)
    try:
        yield value
    finally:
        _snapshot.reset(token)


def scoped_handler(handler):
    async def run(task, services):
        versions, connection_id = None, ""
        if task.kind == "skill.transfer":
            from team_skills.transfer.commands import TransferCommands
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                command = await TransferCommands(services).get(conn, task.payload_ref)
            if command and command["operation"] == "export":
                versions = {v["name"]: [v["version"]] for v in command["payload"]["versions"]}
            if command:
                connection_id = command["payload"].get("connection_id", "")
        elif task.kind in {"skill.validate", "skill.evolve"}:
            from team_skills.evolution.repository import EvolutionRepository
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                command = await EvolutionRepository(services.tx).command(conn, task.payload_ref)
            if command:
                payload = command["payload"]
                connection_id = (payload.get("connection_id") or
                                 payload.get("detail", {}).get("baseline_identity", {}).get("connection_id", ""))
        async with skill_scope(services, task.tenant_id, versions=versions, connection_id=connection_id):
            return await handler(task, services)
    return run
