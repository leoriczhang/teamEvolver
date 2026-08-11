"""Public package surface for teamEvolver shared skill libraries."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import TeamEvolverConfig
    from .config_store import ConfigStore
    from .skills import SkillManager
    from .trajectory_benchmark import (
        amine_benchmark_from_trajectories,
        get_trajectory_benchmark_run,
        list_trajectory_benchmark_runs,
        mine_benchmark_from_trajectories,
    )

__all__ = [
    "TeamEvolverConfig",
    "ConfigStore",
    "SkillManager",
    "mine_benchmark_from_trajectories",
    "amine_benchmark_from_trajectories",
    "list_trajectory_benchmark_runs",
    "get_trajectory_benchmark_run",
]


_EXPORT_MAP = {
    "TeamEvolverConfig": ("teamEvolver.config", "TeamEvolverConfig"),
    "ConfigStore": ("teamEvolver.config_store", "ConfigStore"),
    "SkillManager": ("teamEvolver.skills", "SkillManager"),
    "mine_benchmark_from_trajectories": (
        "teamEvolver.trajectory_benchmark", "mine_benchmark_from_trajectories"
    ),
    "amine_benchmark_from_trajectories": (
        "teamEvolver.trajectory_benchmark", "amine_benchmark_from_trajectories"
    ),
    "list_trajectory_benchmark_runs": (
        "teamEvolver.trajectory_benchmark", "list_trajectory_benchmark_runs"
    ),
    "get_trajectory_benchmark_run": (
        "teamEvolver.trajectory_benchmark", "get_trajectory_benchmark_run"
    ),
}


def __getattr__(name: str):
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module 'teamEvolver' has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
