"""Guard every external Replay operation with the database's current owner."""
from __future__ import annotations

import threading


class GuardedFactory:
    def __init__(self, factory, task, services, slot=None, owner=""):
        self.factory, self.task, self.services = factory, task, services
        self.slot, self.owner = slot, owner
        self.revision = getattr(factory, "revision", "")
        self.sessions = []
        self.lock = threading.Lock()

    def guard(self):
        async def check(conn):
            await self.services.tasks.require_current(conn, self.task)
            if self.slot:
                row = await conn.fetchval(
                    f"SELECT 1 FROM {self.services.tx.pool.schema}.resource_leases "
                    "WHERE resource_key=$1 AND owner_id=$2 AND fence=$3 AND lease_until>clock_timestamp()",
                    self.slot[0], self.owner, self.slot[1],
                )
                if row is None:
                    raise RuntimeError("Replay resource lease lost")
        self.services.tx.run_tenant_tx(self.task.tenant_id, check)

    def open(self, context):
        self.guard()
        session = self.factory.open(context)
        with self.lock:
            if any(session is prior for prior in self.sessions):
                raise RuntimeError("Replay factory reused a Session across branches")
            self.sessions.append(session)
        return GuardedSession(session, self.guard)


class GuardedSession:
    def __init__(self, session, guard):
        self.session, self.guard = session, guard

    def send(self, message):
        self.guard()
        return self.session.send(message)

    def close(self):
        self.session.close()
