# ruff: noqa: F401
"""Lossless source assembly is domain independent and never changes source facts."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_components import SOURCE_RESOURCE_PROTOCOL, assemble_components
from team_miner.skill_miner.native_round import _reviewed_component_catalog
from team_miner.skill_miner.native_source_resources import frozen_source_resources
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT
from team_miner.skill_miner.ov_round_orchestration import _cache_inputs
from tests.miner.skill_miner.support.native_components import SOURCE, package, save_manifest, snapshot

RAW = '# 原始规则\n\n含 "引号"、\\转义与 | 表格。\n同一来源记为0万／20万，未确认。\n'.encode()

def copied_package(root, name="service-recovery"):
    manifest, dataset = package(root, name)
    manifest["schema_version"] = SOURCE_RESOURCE_PROTOCOL
    row = manifest["references"][0]
    row["copy_source"] = SOURCE
    (root / row["path"]).unlink()
    save_manifest(root, manifest)
    return manifest, dataset

def freeze(root, number=1, content=None):
    return _cache_inputs(root / f"round_inputs/round-{number}", [
        {"kind": "source", "relative_path": "original/rules.md", "origin_uri": SOURCE,
         "content": RAW.decode() if content is None else content},
        {"kind": "context", "relative_path": "revision/derived.md", "content": "派生内容"},
        {"kind": "human", "relative_path": "supplements/review.md", "content": "用户复核记录"},
    ])
