# ruff: noqa: F401
"""A later-round duplicate slug cannot overwrite its reviewed workflow or fail the whole job."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import IdentityCatalog, candidate_id
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.candidate_identity import candidate, group_current


def fixture(tmp_path, monkeypatch):
    prior = candidate("referral-request", task="保单递送后请求转介绍")
    current = candidate("referral-request", task="递送保单并确认签收")
    current["boundary"] = "不包含转介绍"
    scope = {"ordinals": [2], "method_ordinals": [2]}
    _save(tmp_path / "plan.json", {"schema_version": "skill-miner.plan.v1", "skills": [current], "deferred": []})
    _save(tmp_path / "scopes/referral-request.json", scope)
    _save(tmp_path / "method-decisions/leaf.json", {"target": "candidate", "name": "referral-request"})
    monkeypatch.setattr(orchestration, "_prior_authorities",
                        lambda job, root: iter([(copy.deepcopy(prior), {"ordinals": [0], "method_ordinals": [0]})]))
    return prior, current, scope
