"""Durable external intents; ambiguous POST results are reconciled before retry."""

from teamEvolver.managed_agents import MAError

from .repository import MARepository


class UnknownEffect(ValueError):
    pass


class Effects:
    def __init__(self, services, tenant_id, connection_id, owner_id, *, task=None):
        self.services, self.tenant, self.connection, self.owner = services, tenant_id, connection_id, owner_id
        self.repo, self.task = MARepository(services.tx), task

    async def guard(self, conn):
        if self.task is not None:
            await self.services.tasks.require_current(conn, self.task)

    async def apply(self, effect_id, kind, intent, send, reconcile=None):
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            await self.guard(conn)
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                               f"{self.tenant}:ma-effect:{effect_id}")
            row, fresh = await self.repo.prepare_effect(
                conn, tenant_id=self.tenant, connection_id=self.connection, effect_id=effect_id,
                owner_id=self.owner, kind=kind, intent=intent,
            )
        if not fresh:
            if row["status"] == "confirmed":
                return row["receipt"]
            if row["status"] == "failed":
                # A definite HTTP rejection did not create a resource.
                fresh = True
            elif reconcile:
                receipt = await reconcile(row)
                if receipt:
                    await self.confirm(effect_id, receipt)
                    return receipt
            if not fresh:
                raise UnknownEffect("External operation outcome is unknown; reconciliation required")
        try:
            async with self.services.tx.tenant_tx(self.tenant) as conn:
                await self.guard(conn)
                if row["status"] == "failed":
                    await self.repo.confirm_effect(conn, effect_id, "", {}, status="outcome_unknown")
            receipt = await send()
        except MAError as exc:
            if exc.code != "TransportOutcomeUnknown" and exc.status < 500:
                async with self.services.tx.tenant_tx(self.tenant) as conn:
                    await self.guard(conn)
                    await self.repo.confirm_effect(conn, effect_id, "", {}, status="failed")
            raise
        await self.confirm(effect_id, receipt)
        return receipt

    async def confirm(self, effect_id, receipt):
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            await self.guard(conn)
            await self.repo.confirm_effect(conn, effect_id, str(receipt.get("id") or ""), receipt)
