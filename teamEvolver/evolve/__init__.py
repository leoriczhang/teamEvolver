"""Built-in team skill evolution engine."""

from typing import Any

from .kernel.enums import FAILURE_LABELS, NO_SKILL_KEY, DecisionAction, FailureType
from .kernel.registry import SkillIDRegistry
from .kernel.settings import EvolveServerConfig

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
        from .runtime.orchestrator import EvolveServer

        return EvolveServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
