"""Paged PG journals, protected by the existing job lease and Worker fence."""

from ..task_io import State


class Journal:
    def __init__(self, engine, job):
        self.engine = engine
        self.prefix = f"{job['job_id']}/{job.get('current_round', 1)}/{job.get('dialogue_recovery_epoch', 0)}"
        self.state = State(engine.services, engine.tenant)

    async def get(self, key):
        async with self.engine.services.tx.tenant_tx(self.engine.tenant) as conn:
            return await self.state.get(conn, "dialogue_dimensions", self.prefix + "/" + key)

    async def save(self, **objects):
        async with self.engine.services.tx.tenant_tx(self.engine.tenant) as conn:
            await self.engine._guard_connection(conn)
            for key, value in objects.items():
                await self.state.put(conn, "dialogue_dimensions", self.prefix + "/" + key, value)
