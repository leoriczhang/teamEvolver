"""Standalone True Replay implementation.

Producer projects submit immutable Test Datasets and Baseline/Candidate
treatments through the public Replay Interface. Historical imports under
``teamEvolver.replay`` remain compatibility aliases.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import ReplaySpec
    from .engine import evaluate_job
    from .gateway import DisabledReplayGateway, EmbeddedReplayGateway
    from .memory import MemoryTrueReplayRunner

__all__ = [
    "DisabledReplayGateway",
    "EmbeddedReplayGateway",
    "MemoryTrueReplayRunner",
    "ReplaySpec",
    "evaluate_job",
]

_EXPORTS = {
    "evaluate_job": ("team_replay.engine", "evaluate_job"),
    "MemoryTrueReplayRunner": (
        "team_replay.memory",
        "MemoryTrueReplayRunner",
    ),
    "ReplaySpec": ("team_replay.contracts", "ReplaySpec"),
    "EmbeddedReplayGateway": (
        "team_replay.gateway",
        "EmbeddedReplayGateway",
    ),
    "DisabledReplayGateway": (
        "team_replay.gateway",
        "DisabledReplayGateway",
    ),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'team_replay' has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
