# ruff: noqa: F401
"""Long integrated drafts need bounded deltas, not repeated whole-body repairs."""

import asyncio
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.artifact_recovery import prepare_recovery
from team_miner.skill_miner.ov_artifact_batch_orchestration import _ledger, _verify
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.skill_body import PROTOCOL as INTEGRATED_PROTOCOL
from team_miner.skill_miner.skill_body import iter_integrated_skill, previous_body
from team_miner.skill_miner.skill_body_insert import PROTOCOL, SCHEMA, body_sha, project, validate
from tests.miner.skill_miner.support.artifact_batches import ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start

POLICY = {"language": "zh-CN"}

def delta(previous, ids):
    if "额外四十分钟须再次征得同意。" in previous["content"]:
        return {"schema_version": SCHEMA, "base_body_sha256": body_sha(previous), "input_ids": ids,
                "title": previous["title"], "insert": [], "no_change_reason": "本批复核未增加新的执行条件。"}
    return {"schema_version": SCHEMA, "base_body_sha256": body_sha(previous), "input_ids": ids,
            "title": previous["title"], "insert": [{"anchor_id": "s0000", "text": "额外四十分钟须再次征得同意。",
                                                    "support_ids": ids}], "no_change_reason": ""}
