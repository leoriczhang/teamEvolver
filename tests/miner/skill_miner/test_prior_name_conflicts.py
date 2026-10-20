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


def test_conflicting_second_round_proposal_has_a_distinct_identity_and_scope(tmp_path, monkeypatch):
    prior, current, scope = fixture(tmp_path, monkeypatch)
    job = {"job_id": "job", "current_round": 2}
    orchestration._seed_catalog(job, tmp_path, "identity", include_reviews=True)
    plan = _read(tmp_path / "plan.json")
    renamed = plan["skills"][0]
    assert renamed["name"] != prior["name"] and len(renamed["name"]) <= 64
    assert {**renamed, "name": current["name"]} == current
    assert _read(tmp_path / "scopes" / (renamed["name"] + ".json")) == scope
    assert _read(tmp_path / "method-decisions/leaf.json")["name"] == renamed["name"]
    db = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
    try:
        old = db.get(candidate_id("job", prior["name"]))
        new = db.get(candidate_id("job", renamed["name"]))
        assert old["candidate"] == prior and not old["selected"]
        assert new["candidate"] == renamed and new["selected"]
        assert old["scope"]["ordinals"] == [0] and new["scope"] == scope
    finally:
        db.close()
    before = (tmp_path / "plan.json").read_bytes()
    orchestration._seed_catalog(job, tmp_path, "identity", include_reviews=True)
    assert (tmp_path / "plan.json").read_bytes() == before


def test_interrupted_conflict_projection_replays_the_same_identity(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    original_io = orchestration._io
    def io():
        parts = list(original_io())
        save = parts[2]
        def fail(path, value):
            if path.parent.name == "method-decisions":
                raise OSError("interrupted projection")
            save(path, value)
        parts[2] = fail
        return tuple(parts)
    monkeypatch.setattr(orchestration, "_io", io)
    with pytest.raises(OSError):
        orchestration._seed_catalog({"job_id": "job", "current_round": 2}, tmp_path, "identity", include_reviews=True)
    journal = _read(tmp_path / "identity/name-conflicts/referral-request.json")
    monkeypatch.setattr(orchestration, "_io", original_io)
    orchestration._seed_catalog({"job_id": "job", "current_round": 2}, tmp_path, "identity", include_reviews=True)
    assert _read(tmp_path / "plan.json")["skills"][0]["name"] == journal["new_name"]


def test_later_grouping_cannot_discard_the_changed_workflow_under_its_old_name(tmp_path, monkeypatch):
    prior, _, _ = fixture(tmp_path, monkeypatch)
    job = {"job_id": "job", "current_round": 2}
    orchestration._seed_catalog(job, tmp_path, "identity", include_reviews=True)
    proposed = _read(tmp_path / "plan.json")["skills"][0]
    db = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
    try:
        rows = [db.get(candidate_id("job", name)) for name in (prior["name"], proposed["name"])]
    finally:
        db.close()
    current, value = group_current(rows)
    orchestration._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json")["skills"] == [proposed]
    assert not _read(tmp_path / "identity/aliases.json")
