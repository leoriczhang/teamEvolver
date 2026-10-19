"""Shared API and Worker dependencies assembled by the application root.

The frozen container holds transaction, task and lease repositories, a handler
registry, and injected domain collaborators. API roles register no handlers;
Worker roles execute the registered handlers. Domain modules receive this
container without importing the application root."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..database.transaction import TransactionManager
from .repository import ResourceLeaseRepository, WorkerTaskRepository
from .tasks import TaskRegistry, build_task_registry

VALID_ROLES = ("api", "worker", "all")


@dataclass(frozen=True)
class RuntimeServices:
    """Long-lived singletons shared by the API and Worker roles."""

    role: str
    tx: TransactionManager
    tasks: WorkerTaskRepository
    leases: ResourceLeaseRepository
    registry: TaskRegistry
    openviking: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def runs_api(self) -> bool:
        return self.role in ("api", "all")

    @property
    def runs_worker(self) -> bool:
        return self.role in ("worker", "all")


def build_services(
    *,
    role: str,
    tx: TransactionManager,
    handlers: dict | None = None,
    openviking: Any = None,
    extras: dict[str, Any] | None = None,
) -> RuntimeServices:
    """Construct the runtime services container for a given role.

    ``role`` must be one of :data:`VALID_ROLES`. The worker roles (``worker`` /
    ``all``) assemble the fixed task registry from the handlers they own; the
    ``api`` role registers no handlers.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid runtime role {role!r}; expected one of {VALID_ROLES}")
    handler_map = dict(handlers or {})
    if role == "api" and handler_map:
        raise ValueError("the 'api' role must not register task handlers (it only enqueues)")
    registry = build_task_registry(handler_map, require_complete=(role == "all"))
    return RuntimeServices(
        role=role,
        tx=tx,
        tasks=WorkerTaskRepository(tx),
        leases=ResourceLeaseRepository(tx),
        registry=registry,
        openviking=openviking,
        extras=dict(extras or {}),
    )
