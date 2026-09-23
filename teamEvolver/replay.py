"""Virtual compatibility package for historical ``teamEvolver.replay`` imports."""

from __future__ import annotations

import sys
from importlib import import_module

from .replay_adapter import ensure_replay_host

ensure_replay_host()

# Let legacy dotted imports resolve without retaining a duplicate replay/
# directory. Every alias points at the canonical module object.
__path__: list[str] = []
_TARGETS = {
    "adapters": "team_replay.adapters",
    "aggregation": "team_replay.aggregation",
    "deap": "team_replay.deap",
    "engine": "team_replay.engine",
    "memory": "team_replay.memory",
    "memory_changes": "team_memory.memory_changes",
    "memory_routes": "team_memory.memory_routes",
    "metrics": "team_replay.metrics",
    "model_broker": "team_replay.model_broker",
    "policy": "team_replay.policy",
    "turn_server": "team_replay.turn_server",
}

for _name, _target in _TARGETS.items():
    _module = import_module(_target)
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

from team_replay import (  # noqa: E402,F401
    DisabledReplayGateway,
    EmbeddedReplayGateway,
    MemoryTrueReplayRunner,
    ReplaySpec,
    evaluate_job,
)

__all__ = [
    "DisabledReplayGateway",
    "EmbeddedReplayGateway",
    "MemoryTrueReplayRunner",
    "ReplaySpec",
    "evaluate_job",
]
