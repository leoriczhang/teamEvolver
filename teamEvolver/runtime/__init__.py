"""API and Worker lifecycle, task execution, leases and shared dependencies.

The runtime imports platform infrastructure only; business handlers are injected
by the application composition root."""

from __future__ import annotations

from .leases import LeaseGuard, LeaseHandle
from .lifecycle import RuntimeLifecycle, build_lifecycle
from .repository import (
    TERMINAL_STATES,
    ClaimedTask,
    ResourceLeaseRepository,
    WorkerTaskRepository,
)
from .services import VALID_ROLES, RuntimeServices, build_services
from .tasks import (
    TASK_KINDS,
    TASK_KINDS_SET,
    HandlerOutcome,
    HandlerResult,
    TaskHandler,
    TaskRegistry,
    build_task_registry,
)
from .worker import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    WorkerStats,
    backoff_for_attempt,
    process_once,
    run_worker,
)

__all__ = [
    "TASK_KINDS",
    "TASK_KINDS_SET",
    "TaskRegistry",
    "TaskHandler",
    "HandlerResult",
    "HandlerOutcome",
    "build_task_registry",
    # repository
    "WorkerTaskRepository",
    "ResourceLeaseRepository",
    "ClaimedTask",
    "TERMINAL_STATES",
    # leases
    "LeaseGuard",
    "LeaseHandle",
    # services
    "RuntimeServices",
    "build_services",
    "VALID_ROLES",
    # lifecycle
    "RuntimeLifecycle",
    "build_lifecycle",
    # worker
    "run_worker",
    "process_once",
    "WorkerStats",
    "backoff_for_attempt",
    "RETRY_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
]
