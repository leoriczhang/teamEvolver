"""team_skills: Session/Trace analysis, Evidence, Skill Candidate, Skill Library,
Publish/Rollback and Agent Skill Sync — the Skill self-evolution domain.

Sibling top-level package to teamEvolver/team_replay/team_memory; ships in the
same distribution and FastAPI process.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from team_skills.candidates.store import ValidationStore
    from team_skills.candidates.worker import ValidationRunSummary, ValidationWorker
    from team_skills.evolution.kernel.settings import EvolveServerConfig
    from team_skills.evolution.runtime.orchestrator import EvolveServer
    from team_skills.library import SkillHub, SkillManager
    from team_skills.library.mutations import (
        SkillMutationCommand,
        SkillMutationService,
    )

__all__ = [
    "EvolveServer",
    "EvolveServerConfig",
    "SkillHub",
    "SkillManager",
    "SkillMutationCommand",
    "SkillMutationService",
    "ValidationStore",
    "ValidationWorker",
    "ValidationRunSummary",
]

_EXPORTS = {
    "EvolveServer": ("team_skills.evolution.runtime.orchestrator", "EvolveServer"),
    "EvolveServerConfig": ("team_skills.evolution.kernel.settings", "EvolveServerConfig"),
    "SkillHub": ("team_skills.library", "SkillHub"),
    "SkillManager": ("team_skills.library", "SkillManager"),
    "SkillMutationCommand": ("team_skills.library.mutations", "SkillMutationCommand"),
    "SkillMutationService": ("team_skills.library.mutations", "SkillMutationService"),
    "ValidationStore": ("team_skills.candidates.store", "ValidationStore"),
    "ValidationWorker": ("team_skills.candidates.worker", "ValidationWorker"),
    "ValidationRunSummary": ("team_skills.candidates.worker", "ValidationRunSummary"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'team_skills' has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *__all__])
