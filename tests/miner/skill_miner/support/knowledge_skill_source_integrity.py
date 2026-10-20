# ruff: noqa: F401
"""Source ambiguity and authored ordering stay inside the versioned Skill script."""
import importlib.util
from pathlib import Path

import pytest

from tests.miner.skill_miner.support import COMPILER_ROOT
from tests.miner.skill_miner.support.knowledge_skill_preparation import Workspace, table

SCRIPT = COMPILER_ROOT / "compiler_skills/knowledge-point-v8/scripts/prepare_point.py"

SPEC = importlib.util.spec_from_file_location("source_integrity_v8", SCRIPT)

PREP = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(PREP)

@pytest.fixture
def documents(tmp_path):
    workspace = Workspace(tmp_path)
    files = {}
    for name, ids in workspace.ids_by_file.items():
        parts = PREP.sources(tmp_path, ids)
        text = PREP.join_ranges(list(parts.items()))
        files[name] = {"text": text, "uri": next(iter(parts.values()))["uri"],
                       "sha256": PREP.digest(text), "range_ids": ids}
    return files
