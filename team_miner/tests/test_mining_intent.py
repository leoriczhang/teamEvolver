"""User scope is frozen and enforced without count-driven fabrication or extra Compile calls."""

from __future__ import annotations

import copy

import pytest

from team_miner.mining_intent import normalize_intent, select_candidates
from team_miner.tests.test_workflow_metadata import _candidate, _group
from team_miner.workflow_metadata import (
    WorkflowMetadataManifestError,
    WorkflowMetadataOutputError,
    build_workflow_metadata,
    validate_workflow_metadata_manifest,
    validate_workflow_metadata_output,
)


def assessment(priority=50, *, matches=True, complete=True):
    return {"matches_direction": matches, "complete_for_goal": complete, "priority": priority,
            "reason": "来源支持指定范围内的完整独立方法"}


@pytest.mark.parametrize("args", [("invalid", "", None), ([], "", None), ("auto", "", 2),
                                  ("single", "", None), ("single", "完整方法", True),
                                  ("target", "", None), ("target", "", True), ("target", "", 1.5),
                                  ("target", "", 101), ("auto", "x" * 4001, None)])
def test_invalid_scope_is_rejected_before_creating_a_job(args):
    with pytest.raises(ValueError):
        normalize_intent(*args)


def plan(names):
    return {"schema_version": "skill-miner.plan.v1", "skills": [
        {"name": name, "task": "完整方法", "source_refs": [f"viking://source/{name}.md"]} for name in names
    ], "deferred": []}


def test_auto_without_direction_preserves_every_supported_method_and_count():
    original = plan([f"method-{index}" for index in range(117)])
    selected, audit = select_candidates(original, normalize_intent(), {}, {})
    assert selected == original and audit["selected_count"] == 117


def test_target_selects_complete_methods_by_priority_and_archives_exclusions():
    original = plan(["first", "best", "unrelated", "second"])
    before = copy.deepcopy(original)
    selected, audit = select_candidates(original, normalize_intent("target", "目标方法", 2),
                                        {"first": assessment(10), "best": assessment(100),
                                         "unrelated": assessment(99, matches=False), "second": assessment(80)}, {})
    assert [row["name"] for row in selected["skills"]] == ["best", "second"]
    assert {row["name"] for row in audit["excluded"]} == {"first", "unrelated"}
    assert original == before and not audit["shortfall"]


def test_single_cannot_choose_one_substep_and_claim_the_whole_goal_complete():
    original = plan(["opening", "collect-information"])
    selected, audit = select_candidates(original, normalize_intent("single", "完整客户沟通流程"),
                                        {name: assessment(100, complete=False) for name in
                                         ("opening", "collect-information")}, {})
    assert not selected["skills"] and audit["shortfall"] == 1
    assert "请求 1" in selected["deferred"][0]["reason"]
    assert selected["deferred"][0]["source_refs"] == sorted(row["source_refs"][0] for row in original["skills"])


def test_count_shortfall_and_zero_method_sources_are_explicit_review_gaps():
    selected, audit = select_candidates(plan([]), normalize_intent("target", "", 3), {}, {},
                                        ["viking://source/original.md"])
    assert audit["shortfall"] == 3 and len(selected["deferred"]) == 1
    assert selected["deferred"][0]["source_refs"] == ["viking://source/original.md"]


def test_alias_survivor_keeps_goal_assessment_without_regenerating_metadata():
    selected, audit = select_candidates(plan(["survivor"]), normalize_intent("single", "完整处理请求"),
                                        {"member": assessment()}, {"member": {"name": "survivor"}})
    assert selected["skills"][0]["name"] == "survivor" and not audit["shortfall"]


def test_transitive_alias_survivor_keeps_goal_assessment():
    selected, audit = select_candidates(plan(["survivor"]), normalize_intent("single", "完整处理请求"),
                                        {"member": assessment()},
                                        {"member": {"name": "intermediate"}, "intermediate": {"name": "survivor"}})
    assert selected["skills"][0]["name"] == "survivor" and not audit["shortfall"]


def test_alias_cycles_are_rejected_instead_of_silently_dropping_an_assessment():
    with pytest.raises(ValueError, match="alias cycle"):
        select_candidates(plan(["survivor"]), normalize_intent("single", "完整处理请求"),
                          {"member": assessment()}, {"member": {"name": "middle"}, "middle": {"name": "member"}})


def test_scope_and_count_are_hash_bound_to_the_existing_metadata_call():
    intent = normalize_intent("target", "完整处理请求", 2)
    controls, manifest = build_workflow_metadata(_group(), mining_intent=intent)
    validate_workflow_metadata_manifest(manifest, controls=controls)
    value = {**_candidate(manifest), "intent": assessment()}
    assert validate_workflow_metadata_output(value, manifest, language_policy={"language": "zh-CN"}) == value
    altered = copy.deepcopy(manifest)
    altered["mining_intent"]["target_count"] = 3
    with pytest.raises(WorkflowMetadataManifestError):
        validate_workflow_metadata_manifest(altered)
    value.pop("intent")
    with pytest.raises(WorkflowMetadataOutputError):
        validate_workflow_metadata_output(value, manifest)


def test_normal_metadata_does_not_accept_an_unsolicited_scope_assessment():
    _, manifest = build_workflow_metadata(_group())
    with pytest.raises(WorkflowMetadataOutputError):
        validate_workflow_metadata_output({**_candidate(manifest), "intent": assessment()}, manifest)


@pytest.mark.asyncio
async def test_scope_selection_uses_the_existing_batch_and_is_restart_safe(tmp_path):
    from team_miner.mining_intent import apply_intent_selection
    from team_miner.ov_staged_orchestration import _read, _save
    from team_miner.ov_workflow_planning import _assignment
    from team_miner.tests.test_ov_workflow_planning import _accept, _entered, _partition_value
    intent = normalize_intent("target", "完整处理请求", 2)
    host, job, state, records = await _entered(tmp_path, 3, mining_intent=intent)
    ledger = host.submissions[-1]
    assert '完整处理请求' in ledger["slots"][0]["packet"]["revision_context"]
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(ledger["slots"][0], group=False)])
    ledger = host.submissions[-1]
    values = []
    for index, slot in enumerate(ledger["slots"]):
        assigned = _assignment(tmp_path, slot["context"])
        assert assigned["manifest"]["mining_intent"] == intent
        value = _candidate(assigned["manifest"])
        value["candidate"]["name"] = f"method-{index}"
        value["intent"] = assessment(20 + index)
        values.append(value)
    await _accept(host, job, tmp_path, state, ledger, values)
    for index, path in enumerate((tmp_path / "source-files").glob("*.json")):
        _save(path, {**_read(path), "uri": records[index]["item"]["source_files"][0]})
    assert len(host.submissions) == 2  # Partition plus existing metadata batch; no selection Compile.
    audit = apply_intent_selection(tmp_path, state)
    assert state["candidate_names"] == ["method-1", "method-2"]
    assert audit["selected_count"] == 2 and not audit["shortfall"]
    assert len(_read(tmp_path / "mining-intent/original-plan.json")["skills"]) == 3
    before = (tmp_path / "plan.json").read_bytes()
    assert apply_intent_selection(tmp_path, state) == audit
    assert (tmp_path / "plan.json").read_bytes() == before
