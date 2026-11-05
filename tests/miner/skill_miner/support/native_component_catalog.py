# ruff: noqa: F401
"""Incremental component context retains accepted ownership without inventing Evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner.native_round import _reviewed_component_catalog
from tests.miner.skill_miner.support.native_components import SOURCE, package, snapshot, write_json


def reviewed_package(tmp_path):
    root = tmp_path / "job"
    reviewed = root / "rounds/round-1/reviewed_output"
    reviewed.mkdir(parents=True)
    manifest, dataset = package(reviewed)
    (reviewed / "analysis").rename(reviewed / "semantic_reports")
    return root, reviewed, manifest, dataset
