"""SkillMiner module for document-to-Skill mining and Benchmark generation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import (
        amine_benchmark_from_trajectories,
        get_trajectory_benchmark_run,
        list_trajectory_benchmark_runs,
        mine_benchmark_from_trajectories,
    )

__all__ = [
    "mine_benchmark_from_trajectories",
    "amine_benchmark_from_trajectories",
    "list_trajectory_benchmark_runs",
    "get_trajectory_benchmark_run",
]

_EXPORT_MAP = {
    name: ("team_miner.api", name)
    for name in __all__
}


def __getattr__(name: str):
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module 'team_miner' has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
