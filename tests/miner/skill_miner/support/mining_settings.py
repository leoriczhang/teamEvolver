# ruff: noqa: F401
from __future__ import annotations

from copy import deepcopy

import pytest

from team_miner.skill_miner.knowledge_compiler import SKILL_BODY
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY
from team_miner.skill_miner.settings import (
    LEGACY_COMPILER_WARNING,
    compiler_contract_version,
    compiler_prompt,
    reset_prompt,
    settings_payload,
    update_settings,
)
