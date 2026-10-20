"""Worker task outcomes and the validated static handler registry.

Business modules own handlers; this module owns task kinds and registration
without importing business packages. Production composition requires a complete
handler map. Tests may explicitly inject a partial map for a bounded worker."""

from __future__ import annotations

from typing import Awaitable, Callable, Mapping, Protocol

from .repository import ClaimedTask

# Declared task kinds validated during production composition.
TASK_KINDS: tuple[str, ...] = (
    # Session Ingestion 
    "session.pull",
    "session.normalize",
    "session.split",
    "session.analyze",
    "session.archive",
    # Skill / Experience 
    "skill.evolve",
    "skill.validate",
    "skill.transfer",
    "experience.sync",
    # Replay / Dataset 
    "replay.run",
    "dataset.run",
    # Memory 
    "memory.aggregate",
    "memory.compile",
    "memory.replay",
    # Team Miner 
    "miner.track_compile",
    "miner.rebuild_index",
    "miner.skill_operation",
    "miner.skill_compile",
)

TASK_KINDS_SET = frozenset(TASK_KINDS)


class TaskHandler(Protocol):
    """A handler executes one claimed attempt and reports its outcome.

    Implementations live in the owning business module. The handler
    receives the claimed task and the runtime services, performs its work, and
    returns a :class:`HandlerResult` describing the next state transition. It
    must not perform the fenced state write itself — the worker loop does, so the
    ``owner_id``/``fence`` guard is applied uniformly.
    """

    async def __call__(self, task: ClaimedTask, services) -> "HandlerResult":  # pragma: no cover - protocol
        ...


class HandlerOutcome:
    """The transition a handler asks the worker loop to apply."""

    SUCCEED = "succeed"
    FAIL = "fail"
    RETRY = "retry"
    WAIT_EXTERNAL = "wait_external"
    RECONCILE = "reconcile"
    CANCELLED = "cancelled"


class HandlerResult:
    """Immutable outcome returned by a :class:`TaskHandler`."""

    __slots__ = ("outcome", "result_ref", "error_code", "upstream_task_id", "backoff_seconds")

    def __init__(
        self,
        outcome: str,
        *,
        result_ref: str = "",
        error_code: str = "",
        upstream_task_id: str = "",
        backoff_seconds: float = 0.0,
    ) -> None:
        self.outcome = outcome
        self.result_ref = result_ref
        self.error_code = error_code
        self.upstream_task_id = upstream_task_id
        self.backoff_seconds = backoff_seconds

    @classmethod
    def succeed(cls, result_ref: str = "") -> "HandlerResult":
        return cls(HandlerOutcome.SUCCEED, result_ref=result_ref)

    @classmethod
    def fail(cls, error_code: str) -> "HandlerResult":
        return cls(HandlerOutcome.FAIL, error_code=error_code)

    @classmethod
    def retry(cls, error_code: str = "", backoff_seconds: float = 0.0) -> "HandlerResult":
        return cls(HandlerOutcome.RETRY, error_code=error_code, backoff_seconds=backoff_seconds)

    @classmethod
    def wait_external(cls, upstream_task_id: str) -> "HandlerResult":
        return cls(HandlerOutcome.WAIT_EXTERNAL, upstream_task_id=upstream_task_id)

    @classmethod
    def reconcile(cls) -> "HandlerResult":
        return cls(HandlerOutcome.RECONCILE)

    @classmethod
    def cancelled(cls) -> "HandlerResult":
        return cls(HandlerOutcome.CANCELLED)


class TaskRegistry:
    """Immutable kind → handler map, validated against the fixed closed set."""

    def __init__(self, handlers: Mapping[str, TaskHandler]) -> None:
        self._handlers = dict(handlers)

    def get(self, kind: str) -> TaskHandler:
        try:
            return self._handlers[kind]
        except KeyError as exc:
            raise KeyError(f"no handler registered for task kind {kind!r}") from exc

    def kinds(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def __contains__(self, kind: object) -> bool:
        return kind in self._handlers


def build_task_registry(
    handlers: Mapping[str, TaskHandler], *, require_complete: bool = False
) -> TaskRegistry:
    """Validate and freeze the handler map against declared task kinds.

    Unknown kinds fail validation. Production composition sets require_complete
    to reject missing handlers; explicit test injection may use a partial map."""
    unknown = sorted(k for k in handlers if k not in TASK_KINDS_SET)
    if unknown:
        raise ValueError(f"Worker task registry: unknown task kind(s) not in the fixed set: {unknown}")
    if require_complete:
        missing = sorted(TASK_KINDS_SET - set(handlers))
        if missing:
            raise ValueError(f"Worker task registry: missing handler(s) for task kind(s): {missing}")
    return TaskRegistry(handlers)


# Convenience alias for handler signatures in business modules.
Handler = Callable[[ClaimedTask, object], Awaitable[HandlerResult]]
