"""Frozen mutation scope for an evolution command; no I/O or configuration writes."""
from __future__ import annotations

import re
from collections.abc import Mapping

ALLOWED_MODULES = ("skill", "prompt")
DEFAULT_ALLOWED_MODULES = ("skill",)


def normalize_allowed_modules(value: object) -> list[str]:
    """Validate an explicit selection and return the stable contract order."""
    if not isinstance(value, list) or not value:
        raise ValueError("allowed_modules must be a nonempty list of skill and/or prompt")
    if any(not isinstance(item, str) or item not in ALLOWED_MODULES for item in value):
        raise ValueError("allowed_modules only supports skill and prompt")
    if len(set(value)) != len(value):
        raise ValueError("allowed_modules must not contain duplicates")
    return [module for module in ALLOWED_MODULES if module in value]


def configured_allowed_modules(config: Mapping) -> list[str]:
    """Resolve a plan's selection without expanding legacy plans to new modules."""
    if "evolution_allowed_modules" not in config:
        return list(DEFAULT_ALLOWED_MODULES)
    return normalize_allowed_modules(config["evolution_allowed_modules"])


def resolved_allowed_modules(payload: Mapping) -> list[str]:
    """Resolve only the command snapshot; old commands always remain Skill-only."""
    if "allowed_modules" not in payload:
        return list(DEFAULT_ALLOWED_MODULES)
    return normalize_allowed_modules(payload["allowed_modules"])


def normalize_execution_profile_id(value: object) -> str:
    """Allow the legacy empty target, but never coerce arbitrary identities."""
    if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,199}", value)):
        raise ValueError("execution_profile_id must be a stable identifier of at most 200 characters")
    return value
