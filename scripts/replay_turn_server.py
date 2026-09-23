"""Compatibility entry point for the canonical Replay turn server."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from team_replay import turn_server as _implementation  # noqa: E402

globals().update(
    {
        name: getattr(_implementation, name)
        for name in dir(_implementation)
        if not name.startswith("_")
    }
)


if __name__ == "__main__":
    _implementation.main()
