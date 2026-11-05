"""Final machine-name ownership survives collisions and interrupted projections."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner import ov_workflow_planning as workflow
from team_miner.skill_miner.ov_staged_orchestration import _read
from tests.miner.skill_miner.support.ov_workflow_planning import _accept, _entered, _partition_value
from tests.miner.skill_miner.support.workflow_metadata import _candidate


async def _accepted_metadata(tmp_path, *, base_name="request-resolution", order="later_input"):
    host, job, state, records = await _entered(tmp_path, 3)
    ledger = host.submissions[-1]
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(ledger["slots"][0], group=False)])
    ledger = host.submissions[-1]
    values = [_candidate(workflow._assignment(tmp_path, slot["context"])["manifest"])
              for slot in ledger["slots"]]
    for value in values:
        value["candidate"]["name"] = base_name
    if order == "later_input":
        # A and B propose the same name; C's exact valid proposal happens to be
        # the final name generated for B. All three workflows must survive.
        values[2]["candidate"]["name"] = (
            base_name[:47] + "-" + ledger["slots"][1]["context"]["assignment_sha256"][:16]
        )
    else:
        # C's first fallback is already the name independently proposed by A.
        values[0]["candidate"]["name"] = (
            base_name[:47] + "-" + ledger["slots"][2]["context"]["assignment_sha256"][:16]
        )
    before = copy.deepcopy(values)
    for slot, value in zip(ledger["slots"], values, strict=True):
        slot["acceptance"] = await host._workflow_accept_slot(job, tmp_path, state, slot, value)
        slot["status"] = "valid"
    assert values == before
    return host, state, ledger, records


def _assert_three_owned_candidates(root, ledger, records):
    plan = _read(root / "plan.json")
    names = [candidate["name"] for candidate in plan["skills"]]
    assert len(names) == len(set(names)) == 3
    assert all(len(name) <= 64 for name in names)
    assert len(list((root / "workflow-planning/names").glob("*.json"))) == 3
    for candidate, slot, record in zip(plan["skills"], ledger["slots"], records, strict=True):
        name = candidate["name"]
        assert _read(root / "workflow-planning/names" / (name + ".json")) == {
            "assignment_sha256": slot["context"]["assignment_sha256"]
        }
        assert candidate["source_refs"] == record["item"]["source_files"]
        assert _read(root / "scopes" / (name + ".json"))["method_ordinals"] == record["ordinals"]
        for leaf in record["leaf_items"]:
            assert _read(root / "method-decisions" / (leaf + ".json"))["name"] == name
    return plan


@pytest.mark.asyncio
@pytest.mark.parametrize("base_name", ["request-resolution", "a" * 64])
@pytest.mark.parametrize("order", ["later_input", "occupied_fallback"])
async def test_three_valid_workflows_keep_distinct_owned_names_without_new_compile(tmp_path, base_name, order):
    host, state, ledger, records = await _accepted_metadata(tmp_path, base_name=base_name, order=order)
    submissions = len(host.submissions)
    host._workflow_apply_batch(tmp_path, state, ledger)
    _assert_three_owned_candidates(tmp_path, ledger, records)
    assert len(host.submissions) == submissions


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", ["after_second_name", "before_commit_journal"])
async def test_name_reservation_and_plan_survive_crash_without_duplicate_or_renamed_candidates(
    tmp_path, monkeypatch, crash,
):
    host, state, ledger, records = await _accepted_metadata(tmp_path)
    before = copy.deepcopy(state)
    original = workflow._immutable
    second_sha = ledger["slots"][1]["context"]["assignment_sha256"]
    interrupted = False

    def crash_once(root, path, value):
        nonlocal interrupted
        if not interrupted and crash == "before_commit_journal" and path.startswith("workflow-planning/commits/"):
            interrupted = True
            raise RuntimeError("simulated interruption before commit journal")
        result = original(root, path, value)
        if (not interrupted and crash == "after_second_name" and path.startswith("workflow-planning/names/")
                and value.get("assignment_sha256") == second_sha):
            interrupted = True
            raise RuntimeError("simulated interruption after final name reservation")
        return result

    monkeypatch.setattr(workflow, "_immutable", crash_once)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        host._workflow_apply_batch(tmp_path, state, ledger)
    assert state == before and interrupted
    reserved_before = {path.name: _read(path) for path in (tmp_path / "workflow-planning/names").glob("*.json")}
    host._workflow_apply_batch(tmp_path, state, ledger)
    plan = _assert_three_owned_candidates(tmp_path, ledger, records)
    for filename, ownership in reserved_before.items():
        assert _read(tmp_path / "workflow-planning/names" / filename) == ownership
    replay_state = copy.deepcopy(before)
    host._workflow_apply_batch(tmp_path, replay_state, ledger)
    assert replay_state == state and _read(tmp_path / "plan.json") == plan
