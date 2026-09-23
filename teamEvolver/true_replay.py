"""Compatibility alias for :mod:`team_replay.engine`."""

from __future__ import annotations

import sys
from importlib import import_module

from teamEvolver.replay_adapter import ensure_replay_host

ensure_replay_host()
_implementation = import_module("team_replay.engine")

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
