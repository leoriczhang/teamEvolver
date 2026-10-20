# ruff: noqa: F401
"""Temporary resource contention must not change a frozen request or extend its budget."""

from datetime import datetime, timedelta, timezone

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.knowledge_compiler import SKILL_BODY
from tests.miner.skill_miner.support.native_mining_rounds import new_owner
