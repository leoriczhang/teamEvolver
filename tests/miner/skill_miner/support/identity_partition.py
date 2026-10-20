# ruff: noqa: F401
"""Fresh partition review uses bounded public Compile calls, without a source-overlap graph."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    IDENTITY_GROUP_SCHEMA_V2,
    IDENTITY_PARTITION_PROTOCOL,
    IdentityCatalog,
    render_identity_partition_compiler,
)
from team_miner.skill_miner.compile_strategy import mining_policy_block
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _package, _read, _save
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from tests.miner.skill_miner.support.candidate_identity import fixture, row
from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner


def state():
    return {
        "execution_protocol": "te-mining-optimized.v1",
        "identity_partition_protocol": IDENTITY_PARTITION_PROTOCOL,
        "source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3,
        "language_policy": {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"},
        "identity_partition_packet_budget": 32000,
        "identity_pass": 0, "identity_cursor": 0,
    }

def output(context, *, merge=False):
    ids = context["identity_ids"]
    return {
        "schema_version": IDENTITY_GROUP_SCHEMA_V2, "candidate_ids": list(ids),
        "groups": ([{"candidate_ids": list(ids), "survivor_candidate_id": ids[-1], "reason": "完整相同流程"}]
                   if merge else [{"candidate_ids": [identity], "survivor_candidate_id": identity,
                                   "reason": "不同独立终态"} for identity in ids]),
    }

def run_windows(root, value, *, merge=False):
    windows = []
    while (window := orchestration._identity_window(root, value)) is not None:
        rows, context = window
        current = {"child": f"partition-{len(windows)}", "context": context}
        orchestration._accept_groups(root, current, output(context, merge=merge))
        value["identity_partition_cursor"] = context["after"]
        windows.append((rows, context))
    return windows

class PartitionCompile(ExtractFake):
    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        if method == "POST" and path == "/api/v1/compile":
            body = kwargs["json"]
            packet_uri = body["from"][0] + "/packet.json"
            if packet_uri not in self.contents:
                return result
            packet = json.loads(self.contents[packet_uri])
            if packet["stage"] == "identity_group":
                value = output({"identity_ids": packet["derived_controls"]["required_candidate_ids"]})
                self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        return result

class PartitionOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root)
        self.fixture.ov = self.ov = PartitionCompile()

    async def _staged_candidate_next(self, job, root, value):
        # Stop this integration at the owned identity boundary; writers have
        # independent transport coverage and no model call is faked as proof.
        assert value["identity_review_complete"]
        return job
