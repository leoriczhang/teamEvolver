"""Virtual compatibility package for historical ``team_memory.replay`` imports."""

from __future__ import annotations

import sys
from importlib import import_module

# Keep old imports working without retaining a second Replay directory.
__path__: list[str] = []
_TARGETS = {
    "debug_routes": "team_memory.debug_routes",
    "memory_changes": "team_memory.memory_changes",
    "memory_replay": "team_replay.memory",
    "routes": "team_memory.memory_routes",
}

for _name, _target in _TARGETS.items():
    _module = import_module(_target)
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

__all__ = sorted(_TARGETS)
