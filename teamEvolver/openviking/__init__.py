"""Platform OpenViking layer.

A thin, business-agnostic client for OpenViking:

- ``client`` — auth headers (single Root Key), Account/User selection,
 connection reuse, bounded retry, and uniform error mapping.
- ``compile`` — Compile submit/status/list/cancel/capabilities/get_skill with a
 unified ``Idempotency-Key`` and a terminal-state enum.
- ``directories`` — idempotent parent-first resource/user directory bootstrap.
- ``schemas`` — request/result envelopes and the terminal-state enum.

Deliberately excluded : Memory/Wiki URI rules and any
success post-processing. Those stay in the owning business modules.
"""

from __future__ import annotations

from .client import OpenVikingClient, OpenVikingError, OpenVikingResult
from .compile import CompileClient
from .directories import directory_ancestors, ensure_directories
from .schemas import (
    TERMINAL_STATES,
    CompileState,
    CompileTask,
    is_terminal,
)

__all__ = [
    "OpenVikingClient",
    "OpenVikingError",
    "OpenVikingResult",
    "CompileClient",
    "CompileState",
    "CompileTask",
    "TERMINAL_STATES",
    "directory_ancestors",
    "ensure_directories",
    "is_terminal",
]
