"""OpenViking request/result envelopes and the terminal-state enum."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompileState(str, Enum):
    """OpenViking Compile task lifecycle states (platform-normalized)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[CompileState] = frozenset(
    {CompileState.SUCCEEDED, CompileState.FAILED, CompileState.CANCELLED}
)

# Raw OpenViking status strings mapped onto the normalized enum. Unknown values
# map to RUNNING (non-terminal) so a reconciler keeps polling rather than
# falsely declaring a terminal outcome.
_RAW_STATE_MAP = {
    "pending": CompileState.PENDING,
    "queued": CompileState.PENDING,
    "submitted": CompileState.PENDING,
    "running": CompileState.RUNNING,
    "in_progress": CompileState.RUNNING,
    "processing": CompileState.RUNNING,
    "succeeded": CompileState.SUCCEEDED,
    "success": CompileState.SUCCEEDED,
    "completed": CompileState.SUCCEEDED,
    "done": CompileState.SUCCEEDED,
    "ok": CompileState.SUCCEEDED,
    "failed": CompileState.FAILED,
    "error": CompileState.FAILED,
    "cancelled": CompileState.CANCELLED,
    "canceled": CompileState.CANCELLED,
    "aborted": CompileState.CANCELLED,
}


def normalize_state(raw: Any) -> CompileState:
    return _RAW_STATE_MAP.get(str(raw or "").strip().lower(), CompileState.RUNNING)


def is_terminal(state: CompileState) -> bool:
    return state in TERMINAL_STATES


@dataclass(frozen=True)
class CompileTask:
    """Normalized view of an OpenViking Compile task."""

    task_id: str
    state: CompileState
    raw_status: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def terminal(self) -> bool:
        return is_terminal(self.state)
