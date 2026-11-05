# ruff: noqa: F401
"""Compiler prompt contract regressions, not a simulation of model compliance.

OV reuses three transforms across five duties and shares the original Skill and
read attachments with its agents. These tests protect the published dispatch and
resource boundaries; a live run must still verify the planner and child records.
"""

import ast
import json
import re

from team_miner.skill_miner.native_compiler import (
    AUXILIARY_FILES,
    EVIDENCE_REVIEW,
    EXTRACT_DISPATCH,
    FILE_SUBMISSION,
    METHOD_PLANNING,
    PLANNING,
    REDUCE_DISPATCH,
    SKILL_BODY,
    SOURCE_OBSERVATION,
    SYNTHESIZE_DISPATCH,
    UNIT_DRAFT,
    UNIT_REVIEW,
    freeze_assets,
    frozen_assets,
    record_format_guidance,
)
