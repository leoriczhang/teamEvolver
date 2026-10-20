# ruff: noqa: F401
"""Protect strict mining validation of the canonical Test Dataset contract."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner import benchmark_format as bf
from team_replay.datasets.schema import normalize_document
from team_replay.datasets.schema import validate_document as validate_canonical_document
from tests.miner.skill_miner.fixtures import build_document


def _document():
    return build_document("demo", [{
        "case_id": "case-1",
        "name": "Verify supplied Evidence",
        "query": "Handle the supplied case",
        "requirements": [f"Evidence condition {index}" for index in range(12)],
        "trajectory_requirements": ["Read the supplied material before deciding"],
        "materials": [{"path": "facts.txt", "content": "verified facts"}],
        "source_session_ids": ["session-1"],
    }])
