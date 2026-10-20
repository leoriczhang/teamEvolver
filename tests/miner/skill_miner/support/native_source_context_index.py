# ruff: noqa: F401
"""Compiler upgrades must not migrate accepted packages or their components."""

import hashlib
import json

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    COMPONENT_PROTOCOL,
    SKILL_BODY,
    SOURCE_CONTEXT_PROTOCOL,
    freeze_assets,
    frozen_assets,
)
