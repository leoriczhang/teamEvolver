"""Built-in team skill evolution engine."""

from typing import Any

from team_skills.evolution.kernel.enums import FAILURE_LABELS, NO_SKILL_KEY, DecisionAction, FailureType
from team_skills.evolution.kernel.registry import SkillIDRegistry
from team_skills.evolution.kernel.settings import EvolveServerConfig

__all__ = [
    "EvolveServer",
    "EvolveServerConfig",
    "SkillIDRegistry",
    "FailureType",
    "DecisionAction",
    "FAILURE_LABELS",
    "NO_SKILL_KEY",
]


def __getattr__(name: str) -> Any:
    if name == "EvolveServer":
        from team_skills.evolution.runtime.orchestrator import EvolveServer

        return EvolveServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
