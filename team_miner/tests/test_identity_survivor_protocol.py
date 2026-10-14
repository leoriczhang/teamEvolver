"""Explicit existing survivors preserve workflow identity without rewriting business fields."""

from __future__ import annotations

import asyncio
import copy
import hashlib

import pytest

from team_miner import candidate_identity_orchestration as orchestration
from team_miner.candidate_identity import (
    GROUP_SCHEMA,
    IDENTITY_FIELDS,
    IDENTITY_GROUP_PROTOCOL_V2,
    IDENTITY_GROUP_SCHEMA_V2,
    IdentityCatalog,
    identity_brief,
    validate_identity_groups,
)
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _prior_candidate_rows, _read, _save
from team_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from team_miner.tests.test_candidate_identity import fixture, group_current, row


def explicit_group(rows, survivor, *, child="explicit-group"):
    current, value = group_current(rows, child)
    current["context"]["identity_group_protocol"] = IDENTITY_GROUP_PROTOCOL_V2
    value["schema_version"] = IDENTITY_GROUP_SCHEMA_V2
    value["groups"][0]["survivor_candidate_id"] = survivor["candidate_id"]
    return current, value


def test_later_existing_parent_is_survivor_without_business_metadata_rewrite(tmp_path):
    child = row(0, name="request-confirmation", task="确认客户的具体请求")
    parent = row(1, name="request-resolution", task="核对请求、执行处理并交付处理结论")
    child["candidate"]["source_refs"] = ["viking://source/confirmation.md"]
    parent["candidate"]["source_refs"] = ["viking://source/resolution.md"]
    canonical = copy.deepcopy(parent["candidate"])
    fixture(tmp_path, [child, parent])
    current, value = explicit_group([child, parent], parent)

    orchestration._accept_groups(tmp_path, current, value)

    candidate = _read(tmp_path / "plan.json")["skills"][0]
    assert len(_read(tmp_path / "plan.json")["skills"]) == 1
    assert {key: candidate[key] for key in IDENTITY_FIELDS} == {key: canonical[key] for key in IDENTITY_FIELDS}
    assert candidate["source_refs"] == ["viking://source/confirmation.md", "viking://source/resolution.md"]
    assert _read(tmp_path / "scopes/request-resolution.json")["method_ordinals"] == [0, 1]
    alias = _read(tmp_path / "identity/aliases.json")["request-confirmation"]
    assert (alias["name"], alias["candidate_id"]) == (parent["candidate"]["name"], parent["candidate_id"])
    journal = _read(tmp_path / "identity/journals/explicit-group.json")
    assert journal["requested_groups"][0]["survivor_candidate_id"] == parent["candidate_id"]
    db = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
    try:
        assert not db.is_active(child["candidate_id"])
        assert db.is_active(parent["candidate_id"])
    finally:
        db.close()


@pytest.mark.parametrize("change", ["unknown", "outside_group", "missing_nominee", "business_field",
                                   "missing_header", "duplicate_header", "duplicate_group", "omitted_member"])
def test_explicit_survivor_still_requires_exact_partition_and_known_nominee(change):
    rows = [row(0), row(1)]
    current, value = explicit_group(rows, rows[1])
    if change == "unknown":
        value["groups"][0]["survivor_candidate_id"] = "candidate-unknown"
    elif change == "outside_group":
        value["groups"] = [
            {"candidate_ids": [rows[0]["candidate_id"]], "survivor_candidate_id": rows[1]["candidate_id"],
             "reason": "独立目的"},
            {"candidate_ids": [rows[1]["candidate_id"]], "survivor_candidate_id": rows[1]["candidate_id"],
             "reason": "独立目的"},
        ]
    elif change == "missing_nominee":
        value["groups"][0].pop("survivor_candidate_id")
    elif change == "business_field":
        value["groups"][0]["task"] = "偷偷修改业务方法"
    elif change == "missing_header":
        value["candidate_ids"].pop()
    elif change == "duplicate_header":
        value["candidate_ids"].append(value["candidate_ids"][0])
    elif change == "duplicate_group":
        value["groups"].append(copy.deepcopy(value["groups"][0]))
    else:
        value["groups"][0]["candidate_ids"] = [rows[1]["candidate_id"]]
    with pytest.raises(ValueError):
        validate_identity_groups(value, allowed_ids=current["context"]["identity_ids"], allow_survivor=True)


def test_v1_contract_and_earliest_survivor_are_unchanged(tmp_path):
    rows = [row(0, name="early-child"), row(1, name="later-parent")]
    fixture(tmp_path, rows)
    current, legacy = group_current(rows)
    legacy["groups"][0]["candidate_ids"].reverse()
    assert legacy["schema_version"] == GROUP_SCHEMA
    assert validate_identity_groups(legacy, allowed_ids=current["context"]["identity_ids"]) == legacy["groups"]
    orchestration._accept_groups(tmp_path, current, legacy)
    assert _read(tmp_path / "plan.json")["skills"][0]["name"] == "early-child"
    _, v2 = explicit_group(rows, rows[1])
    with pytest.raises(ValueError):
        validate_identity_groups(v2, allowed_ids=current["context"]["identity_ids"])
    with pytest.raises(ValueError):
        validate_identity_groups(legacy, allowed_ids=current["context"]["identity_ids"], allow_survivor=True)


def test_wrong_prior_nominee_keeps_both_existing_identities(tmp_path):
    rows = [row(0, name="new-workflow"), row(1, name="reviewed-workflow", prior=True)]
    fixture(tmp_path, rows)
    original = _read(tmp_path / "plan.json")
    current, value = explicit_group(rows, rows[0])
    orchestration._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json") == original
    assert _read(tmp_path / "identity/aliases.json") == {}
    journal = _read(tmp_path / "identity/journals/explicit-group.json")
    assert len(journal["groups"]) == 2
    assert all(group["survivor_candidate_id"] == group["candidate_ids"][0] for group in journal["groups"])
    assert journal["host_splits"][0]["survivor_candidate_id"] == rows[1]["candidate_id"]


def test_distinct_prior_authorities_are_not_collapsed_by_explicit_nominee(tmp_path):
    rows = [row(0, name="prior-a", prior=True), row(1, name="prior-b", prior=True), row(2, name="new-c")]
    fixture(tmp_path, rows)
    original = _read(tmp_path / "plan.json")
    current, value = explicit_group(rows, rows[0])
    orchestration._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json") == original
    journal = _read(tmp_path / "identity/journals/explicit-group.json")
    assert len(journal["groups"]) == 3
    assert len(journal["host_splits"]) == 1


def test_prior_survivor_retains_name_and_exact_custom_case_lookup(tmp_path):
    root = tmp_path / "round_inputs/round-2/staged"
    rows = [row(0, name="new-child"), row(7, name="reviewed-parent", prior=True)]
    rows[1]["selected"] = False
    fixture(root, rows)
    _save(root / "plan.json", {"schema_version": "skill-miner.plan.v1",
                               "skills": [rows[0]["candidate"]], "deferred": []})
    folder = root.parent / "previous-candidates"
    ids = [f"reviewed-custom-case:{index}" for index in range(12)]
    value = {"name": "reviewed-parent", "case_ids": ids, "scope_relative_paths": ["original/source.md"]}
    _save(folder / "reviewed-parent.json", value)
    body = (folder / "reviewed-parent.json").read_bytes()
    _save(folder / "manifest.json", [{"name": "reviewed-parent",
                                     "local_path": str(folder / "reviewed-parent.json"),
                                     "sha256": hashlib.sha256(body).hexdigest()}])
    current, result = explicit_group(rows, rows[1])
    orchestration._accept_groups(root, current, result)
    survivor = _read(root / "plan.json")["skills"][0]
    assert survivor["name"] == "reviewed-parent"
    assert {key: survivor[key] for key in IDENTITY_FIELDS} == {
        key: rows[1]["candidate"][key] for key in IDENTITY_FIELDS
    }
    assert next(_prior_candidate_rows(root, name=survivor["name"]))["case_ids"] == ids
    assert (folder / "reviewed-parent.json").read_bytes() == body


def test_explicit_survivor_journal_recovers_after_partial_commit(tmp_path, monkeypatch):
    rows = [row(0, name="early-child"), row(1, name="later-parent")]
    fixture(tmp_path, rows)
    current, value = explicit_group(rows, rows[1])
    original_io = orchestration._io
    original_save = original_io()[2]

    def fail_plan(path, content):
        if path == tmp_path / "plan.json":
            raise OSError("interrupted final plan write")
        return original_save(path, content)

    monkeypatch.setattr(orchestration, "_io", lambda: (*original_io()[:2], fail_plan, *original_io()[3:]))
    with pytest.raises(OSError, match="interrupted"):
        orchestration._accept_groups(tmp_path, current, value)
    assert (tmp_path / "identity/journals/explicit-group.json").is_file()
    monkeypatch.setattr(orchestration, "_io", original_io)
    orchestration._accept_groups(tmp_path, current, value)
    plan = _read(tmp_path / "plan.json")
    orchestration._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json") == plan
    assert plan["skills"][0]["name"] == "later-parent"
    assert _read(tmp_path / "identity/aliases.json")["early-child"]["name"] == "later-parent"
    assert (tmp_path / "identity/journals/explicit-group.committed").is_file()


def test_authority_briefs_are_only_added_for_new_protocol():
    old = row(5)
    assert "authority_priority" not in identity_brief(old)
    assert identity_brief(old, include_authority=True)["authority_priority"] == [1, 5]
    old["prior"] = True
    assert identity_brief(old, include_authority=True)["authority_priority"] == [0, 5]


@pytest.mark.parametrize("fresh", [False, True])
def test_identity_mixin_freezes_the_protocol_and_corresponding_renderer(tmp_path, monkeypatch, fresh):
    rows = [row(0), row(1)]
    fixture(tmp_path, rows)
    (tmp_path / "compiler-base.md").write_text(SKILL_BODY, encoding="utf-8")
    monkeypatch.setattr(orchestration, "_seed_catalog", lambda *args, **kwargs: None)
    state = {"execution_protocol": "te-mining-optimized.v1", "phase": "identity_group",
             "estimated_tasks": 0, "language_policy": {"language": "zh-CN"}}
    if fresh:
        state["source_scope_protocol"] = SOURCE_SCOPE_PROTOCOL_V3

    class Host(orchestration.CandidateIdentityMixin):
        async def _staged_submit(self, job, root, state, stage, output, chunks, controls, context):
            self.captured = controls, context
            return job

    host = Host()
    asyncio.run(host._staged_identity_next({"job_id": "job"}, tmp_path, state))
    controls, context = host.captured
    compiler = (tmp_path / "aux-compilers/identity_group.md").read_text()
    if fresh:
        assert context["identity_group_protocol"] == IDENTITY_GROUP_PROTOCOL_V2
        assert controls["identity_group_protocol"] == IDENTITY_GROUP_PROTOCOL_V2
        assert all(item["authority_priority"] == [1, index] for index, item in enumerate(controls["identities"]))
        assert IDENTITY_GROUP_SCHEMA_V2 in compiler
        assert "The host chooses the authoritative survivor" not in compiler
        assert GROUP_SCHEMA not in compiler
    else:
        assert "identity_group_protocol" not in context
        assert all("authority_priority" not in item for item in controls["identities"])
        assert GROUP_SCHEMA in compiler
        assert IDENTITY_GROUP_SCHEMA_V2 not in compiler


def test_fresh_host_does_not_reinterpret_a_frozen_v1_child(tmp_path):
    rows = [row(0, name="early-child"), row(1, name="later-parent")]
    fixture(tmp_path, rows)
    current, value = group_current(rows)
    state = {"source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3, "identity_cursor": 0}
    asyncio.run(orchestration.CandidateIdentityMixin()._staged_identity_accept({}, tmp_path, state, current, value))
    assert _read(tmp_path / "plan.json")["skills"][0]["name"] == "early-child"
    assert state["identity_cursor"] == current["context"]["after"]
