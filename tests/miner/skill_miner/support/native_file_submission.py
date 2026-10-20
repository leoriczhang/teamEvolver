# ruff: noqa: F401
"""Submission guidance uses the deployed scratch-reference contract without rewriting frozen jobs."""

import hashlib
import json
from unittest.mock import patch

import pytest

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    FILE_SUBMISSION,
    OUTPUT_CONTRACT,
    SKILL_BODY,
    UNIT_REVIEW,
    frozen_assets,
)
from tests.miner.skill_miner.support.native_mining_rounds import new_owner
