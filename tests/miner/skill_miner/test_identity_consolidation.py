"""Reviewed IDs are preserved lineage, not a reason to split an already complete parent workflow."""

import copy

import pytest

from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    CONSOLIDATING_PARTITION_PROTOCOL,
    render_identity_partition_compiler,
)
from team_miner.skill_miner.identity_components import controls, digest
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.candidate_identity import fixture, row
from tests.miner.skill_miner.support.identity_partition import output, state


def assignment(root, rows):
    fixture(root, rows)
    value = state()
    value["identity_partition_protocol"] = CONSOLIDATING_PARTITION_PROTOCOL
    _, context = orchestration._identity_window(root, value)
    return {"child": "consolidated-group", "context": context}


def test_prior_delivery_identities_merge_with_complete_immutable_member_contracts(tmp_path):
    rows = [row(0, name="deliver-policy", prior=True), row(1, name="confirm-delivery", prior=True),
            row(2, name="courier-objection")]
    rows[0]["candidate"]["task"] = "签收、讲解、回访并处理快递异议的完整保单递送流程"
    rows[1]["candidate"]["boundary"] = "电子保单签收后告知15天犹豫期，先征得30分钟讲解同意"
    original = copy.deepcopy(rows)
    current = assignment(tmp_path, rows)
    response = output(current["context"], merge=True)
    response["groups"][0]["survivor_candidate_id"] = rows[0]["candidate_id"]
    orchestration._accept_groups(tmp_path, current, response)
    plan = _read(tmp_path / "plan.json")
    assert len(plan["skills"]) == 1 and plan["skills"][0]["name"] == "deliver-policy"
    journal = _read(tmp_path / "identity/journals/consolidated-group.json")
    assert not journal["host_splits"]
    members = journal["survivors"][0]["consolidated_members"]
    assert {item["candidate_id"] for item in members} == {item["candidate_id"] for item in rows}
    for original_row in original:
        item = next(item for item in members if item["candidate_id"] == original_row["candidate_id"])
        assert item["candidate"] == original_row["candidate"] and item["scope"] == original_row["scope"]
    passed = controls(tmp_path, "deliver-policy")
    assert passed["component_contracts_are_evidence"] is False
    assert original[1]["candidate"] in passed["component_contracts"]
    assert _read(tmp_path / "scopes/deliver-policy.json")["method_ordinals"] == [0, 1, 2]
    before = _read(tmp_path / "identity/components/deliver-policy.json")
    orchestration._accept_groups(tmp_path, current, response)
    assert before == _read(tmp_path / "identity/components/deliver-policy.json")
    assert rows == original


def test_accepted_refinement_is_retained_in_parent_instead_of_forcing_a_split(tmp_path):
    rows = [row(0, name="parent", prior=True), row(1, name="questionnaire")]
    rows[1]["candidate"]["boundary"] = "收集财务信息前必须承诺保密，客户拒绝则停止"
    current = assignment(tmp_path, rows)
    _save(tmp_path / "identity/name-conflicts/refinement.json", {"new_candidate_id": rows[1]["candidate_id"]})
    response = output(current["context"], merge=True)
    response["groups"][0]["survivor_candidate_id"] = rows[0]["candidate_id"]
    orchestration._accept_groups(tmp_path, current, response)
    assert len(_read(tmp_path / "plan.json")["skills"]) == 1
    assert rows[1]["candidate"] in controls(tmp_path, "parent")["component_contracts"]


def test_independent_workflows_remain_singletons_and_no_count_target_is_used(tmp_path):
    rows = [row(0, name="opening", prior=True), row(1, name="delivery", prior=True)]
    current = assignment(tmp_path, rows)
    orchestration._accept_groups(tmp_path, current, output(current["context"]))
    assert len(_read(tmp_path / "plan.json")["skills"]) == 2
    assert controls(tmp_path, "opening") == {}
    compiler = render_identity_partition_compiler(SKILL_BODY, protocol=CONSOLIDATING_PARTITION_PROTOCOL)
    assert "keep different prior authorities separate" not in compiler
    assert "not proof of independent workflows" in compiler
    assert "never merge only to reach a count" in compiler


def test_component_mutation_fails_closed(tmp_path):
    rows = [row(0), row(1)]
    current = assignment(tmp_path, rows)
    orchestration._accept_groups(tmp_path, current, output(current["context"], merge=True))
    path = tmp_path / "identity/components" / (rows[-1]["candidate"]["name"] + ".json")
    original = _read(path)
    assert original["members_sha256"] == digest(original["members"])
    original["members"][0]["candidate"]["boundary"] = "伪造条件"
    _save(path, original)
    with pytest.raises(ValueError, match="changed"):
        controls(tmp_path, rows[-1]["candidate"]["name"])


def test_new_consolidation_policy_cannot_be_injected_into_a_frozen_v2_request(tmp_path):
    rows = [row(0), row(1)]
    current = assignment(tmp_path, rows)
    current["context"]["identity_partition_protocol"] = "te-candidate-identity-partition.v2"
    with pytest.raises(ValueError, match="consolidation protocol"):
        orchestration._accept_groups(tmp_path, current, output(current["context"], merge=True))
