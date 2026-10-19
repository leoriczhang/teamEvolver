"""Resource leases and fencing for the worker.

``resource_leases`` bounds per-tenant concurrency and gives exclusive access to
targets such as a Memory directory or an experience stream so only one worker acts
on them across pods. Leases use database time; a worker that cannot renew must
not start new external side effects.

This module exposes a thin :class:`LeaseGuard` over
:class:`teamEvolver.runtime.repository.ResourceLeaseRepository`: acquire a lease,
keep it alive with ``heartbeat``, and learn (via a ``False`` heartbeat) that the
lease was lost so the caller can abort before causing a duplicate side effect.
The persistence lives in the repository; this layer is the small, reusable
protocol handlers use.
"""

from __future__ import annotations

from dataclasses import dataclass

from .repository import ResourceLeaseRepository


@dataclass(frozen=True)
class LeaseHandle:
    """A held lease: its target and the fence proving who the current owner is."""

    tenant_id: str
    resource_key: str
    owner_id: str
    fence: int


class LeaseGuard:
    """Acquire / renew / release an exclusive lease on a tenant resource."""

    def __init__(self, repo: ResourceLeaseRepository) -> None:
        self._repo = repo

    async def acquire(
        self, conn, *, tenant_id: str, resource_key: str, owner_id: str, lease_seconds: float
    ) -> LeaseHandle | None:
        """Try to take the lease; ``None`` means another live owner holds it."""
        fence = await self._repo.acquire(
            conn,
            tenant_id=tenant_id,
            resource_key=resource_key,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )
        if fence is None:
            return None
        return LeaseHandle(tenant_id=tenant_id, resource_key=resource_key, owner_id=owner_id, fence=fence)

    async def heartbeat(self, conn, handle: LeaseHandle, *, lease_seconds: float) -> bool:
        """Renew *handle*; ``False`` means the lease was lost — stop side effects."""
        return await self._repo.heartbeat(
            conn,
            tenant_id=handle.tenant_id,
            resource_key=handle.resource_key,
            owner_id=handle.owner_id,
            fence=handle.fence,
            lease_seconds=lease_seconds,
        )

    async def release(self, conn, handle: LeaseHandle) -> bool:
        """Release *handle* (best-effort; expiry would free it anyway)."""
        return await self._repo.release(
            conn,
            tenant_id=handle.tenant_id,
            resource_key=handle.resource_key,
            owner_id=handle.owner_id,
            fence=handle.fence,
        )
