# ruff: noqa: F401
"""Explicit existing survivors preserve workflow identity without rewriting business fields."""

from __future__ import annotations

import asyncio
import copy
import hashlib

import pytest

from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    GROUP_SCHEMA,
    IDENTITY_FIELDS,
    IDENTITY_GROUP_PROTOCOL_V2,
    IDENTITY_GROUP_SCHEMA_V2,
    IdentityCatalog,
    identity_brief,
    validate_identity_groups,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _prior_candidate_rows, _read, _save
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from tests.miner.skill_miner.support.candidate_identity import fixture, group_current, row


def explicit_group(rows, survivor, *, child="explicit-group"):
    current, value = group_current(rows, child)
    current["context"]["identity_group_protocol"] = IDENTITY_GROUP_PROTOCOL_V2
    value["schema_version"] = IDENTITY_GROUP_SCHEMA_V2
    value["groups"][0]["survivor_candidate_id"] = survivor["candidate_id"]
    return current, value
