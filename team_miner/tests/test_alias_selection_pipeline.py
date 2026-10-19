"""Merged members follow their survivor through count/direction selection and exact writer routing."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_miner import mining_intent
from team_miner.intent_recovery import ERROR, PROTOCOL, prepare_recovery
from team_miner.ov_compile_mining import SKILL_BODY, utc_now
from team_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.tests.test_fresh_exact_pipeline import FreshOwner, run
from team_miner.tests.test_source_unit_extraction import POLICY


def owner_with_excluded_merged_members(root, mode):
    owner = FreshOwner(root, rounds=2 if mode == "single" else 1, mode=mode,
                       direction="完整客户沟通对练方法", target_count=1 if mode == "target" else None)
    owner.ov.partition_groups = 4
    owner.ov.identity_pair_groups = True
    owner.ov.unmatched_second_pair = mode == "auto"
    return owner


@pytest.mark.parametrize("mode", ["single", "target", "auto"])
@pytest.mark.asyncio
async def test_real_host_merge_then_scope_selection_completes_body_cases_and_adjacent_round(tmp_path, mode):
    owner = owner_with_excluded_merged_members(tmp_path, mode)
    job, entries = owner.new()
    first = await run(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""), 1)
    assert first["status"] == ("running" if mode == "single" else "succeeded"), first.get("error")
    root, _ = await owner._staged_load(first)
    selected = {row["name"] for row in _read(root / "plan.json")["skills"]}
    receipts = [_read(path) for path in (root / "mining-intent/method-selection").glob("*.json")]
    assert receipts and len(selected) == 1
    assert any(json.loads(row["original_text"])["name"] != row["resolved_name"] for row in receipts)
    assert all(row["resolved_name"] not in selected and not row["selection_is_factual_evidence"] for row in receipts)
    for path in (root / "method-decisions").glob("*.json"):
        row = _read(path)
        if row["target"] == "candidate":
            assert mining_intent.resolve_candidate_name(row["name"], _read(root / "identity/aliases.json")) in selected
    before = {path: path.read_bytes() for path in (root / "method-decisions").glob("*.json")}
    mining_intent.apply_intent_selection(root, _read(root / "state.json"))
    assert all(path.read_bytes() == raw for path, raw in before.items())
    if mode == "single":
        second = owner.repo.prepare_automatic_next_round(first["job_id"])
        entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, second, staged=True)
        owner.ov.directories.add(second["source_snapshot"].rsplit("/", 1)[0])
        second = owner.repo.update_job(second["job_id"], input_manifest=str(
            owner.repo._job_dir(second["job_id"]) / "round_inputs/round-2/manifest.json"))
        final = await run(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""), 2)
        assert final["status"] == "succeeded", final.get("error")
    else:
        final = first
    assert final["candidate_count"] == 1
    for number in range(1, len(final["rounds_history"]) + 1):
        packages = list((owner.repo._job_dir(final["job_id"]) /
                         f"rounds/round-{number}/output/compiled_skill").glob("*/benchmark.json"))
        assert len(packages) == 1 and len(_read(packages[0])["cases"]) == 12


@pytest.mark.asyncio
async def test_same_job_recovers_alias_selection_failure_without_rerunning_accepted_native_work(tmp_path, monkeypatch):
    owner = owner_with_excluded_merged_members(tmp_path, "target")
    job, entries = owner.new()

    def old_selection(root, plan, audit, aliases, *, apply=False):
        if apply:
            excluded = {row["name"]: row["reason"] for row in audit["excluded"]}
            for path in (root / "method-decisions").glob("*.json"):
                row = _read(path)
                if row.get("target") == "candidate" and row["name"] in excluded:
                    _save(path, {"target": "knowledge", "name": "", "reason": excluded[row["name"]]})
        return []

    with monkeypatch.context() as old:
        old.setattr(mining_intent, "method_selection_changes", old_selection)
        with pytest.raises(ValueError, match=ERROR):
            await run(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""), 1)
    failed = owner.repo.update_job(job["job_id"], status="failed", error=ERROR, finished_at=utc_now(),
                                  ov_status="completed", ov_task_id="")
    root, state = await owner._staged_load(failed)
    immutable = {path: path.read_bytes() for folder in ("children", "units", "catalog", "workflow-planning", "identity")
                 for path in (root / folder).rglob("*") if path.is_file()}
    calls = list(owner.ov.stages)
    selection = (root / "mining-intent/selection.json").read_bytes()
    dry = prepare_recovery(root, state, failed)
    assert dry["method_changes"] and dry["protocol"] == PROTOCOL
    altered = copy.deepcopy(state)
    altered["cursor"] = 1
    with pytest.raises(ValueError, match="失败边界"):
        prepare_recovery(root, altered, failed)
    prepare_recovery(root, state, failed, apply=True)
    # A crash between journaling the local replay and changing the parent status is recoverable.
    assert prepare_recovery(root, _read(root / "state.json"), failed) == dry
    assert owner.repo.public_job(failed)["can_resume"]
    recovered = owner.repo.resume_identity_failure(failed["job_id"])
    final = await run(owner, await owner._resume_staged_round(recovered), 1)
    assert final["status"] == "succeeded", final.get("error")
    assert final["recovery_history"][-1]["recovery_protocol"] == PROTOCOL
    assert all(path.read_bytes() == raw for path, raw in immutable.items())
    final_selection = _read(root / "mining-intent/selection.json")
    assert all(final_selection[key] == value for key, value in json.loads(selection).items())
    for stage in ("extract", "workflow_partition", "workflow_metadata", "identity_group"):
        assert owner.ov.stages.count(stage) == calls.count(stage)
    for change in dry["method_changes"]:
        receipt = _read(root / "mining-intent/method-selection" / (change["path"].split("/")[-1]))
        assert hashlib.sha256(receipt["original_text"].encode()).hexdigest() == receipt["original_sha256"]
        assert _read(root / change["path"]) == receipt["selected_disposition"]


def test_changed_selection_receipt_cannot_change_a_method_or_reenter_the_final_plan(tmp_path):
    plan = {"skills": [{"name": "kept"}, {"name": "excluded"}]}
    audit = {"excluded": [{"name": "excluded", "reason": "当前用户只选择另一个完整方法"}]}
    aliases = {"member": {"name": "middle"}, "middle": {"name": "excluded"}}
    path = tmp_path / "method-decisions/item-1.json"
    _save(path, {"target": "candidate", "name": "member", "reason": "原方法有来源支持"})
    changes = mining_intent.method_selection_changes(tmp_path, plan, audit, aliases, apply=True)
    assert len(changes) == 1
    receipt = tmp_path / "mining-intent/method-selection/item-1.json"
    frozen = _read(receipt)
    frozen["selected_disposition"] = {"target": "candidate", "name": "kept", "reason": "错误重新选入"}
    _save(receipt, frozen)
    with pytest.raises(ValueError, match="Frozen method selection"):
        mining_intent.method_selection_changes(tmp_path, plan, audit, aliases, apply=True)
