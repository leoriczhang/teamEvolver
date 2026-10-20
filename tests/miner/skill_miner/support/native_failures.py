# ruff: noqa: F401
"""Runtime dependency failures do not become more content-generation requests."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_failures import classify_native_failure
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

COMPILER = "viking://agent/skills/frozen-compiler"

SOURCE = "viking://resources/frozen/source"
