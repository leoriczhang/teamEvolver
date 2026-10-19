"""Durable grouping-to-metadata projections keep every original method's scope."""

from __future__ import annotations

import copy

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.direct_workflow_planning import FrozenPlanningIntegrityError
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _json, _read, _save, _sha, _write
from team_miner.ov_workflow_planning import OvWorkflowPlanningMixin, _assignment, _feedback
from team_miner.tests.test_workflow_metadata import _candidate, _record
from team_miner.workflow_partition import CAPACITY_SCHEMA, OUTPUT_SCHEMA
from team_miner.workflow_partition_compiler import METADATA_STAGE, PARTITION_STAGE


class Host(OvWorkflowPlanningMixin):
    def __init__(self):
        self.submissions = []

    async def _ensure_aux_compiler(self, root, state, stage, body):
        state.setdefault("aux_compilers", {})[stage] = _sha(body)
        _write(root / "aux-compilers" / (stage + ".md"), body)

    async def _staged_save(self, job, root, state, **changes):
        _save(root / "state.json", state)
        return {**job, **changes}

    async def _submit_workflow_batch(self, job, root, state, path, ledger):
        self.submissions.append(copy.deepcopy(ledger))
        return job

    async def _staged_selection_complete(self, job, root, state):
        state["phase"] = "skill_section"
        return job


async def _entered(tmp_path, count=4, *, mining_intent=None):
    host = Host()
    records = [_record(i) for i in range(count)]
    for i, record in enumerate(records):
        _save(tmp_path / "catalog/method" / f"{i:08d}.json", record)
        for uri in record["item"]["source_files"]:
            _save(tmp_path / "source-files" / (_sha(uri) + ".json"), {"ordinals": record["ordinals"]})
    state = {"language_policy": {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"},
             "round": 1, "base_sha256": _sha(SKILL_BODY),
             "catalog_counts": {"method": count}, "selection_cursor": 0, "workflow_partition_protocol":
                 "te-workflow-partition.v2"}
    job = {"job_id": "workflow-test", "current_round": 1}
    if mining_intent is not None:
        state["mining_intent"] = mining_intent
        job["mining_intent"] = mining_intent
    _write(tmp_path / "compiler-base.md", SKILL_BODY)
    await host._workflow_enter(job, tmp_path, state)
    return host, job, state, records


def _partition_value(slot, *, group=True, capacity=False):
    controls = slot["packet"]["derived_controls"]
    ids = controls["assigned_ids"]
    base = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"]}
    if capacity:
        return {**base, "schema_version": CAPACITY_SCHEMA, "input_ids": ids, "reason": "完整说明需要较小输入范围"}
    grouped = group and len(ids) > 1
    return {**base, "rows": [[identity, ids[-1] if grouped else identity] for identity in ids],
            "reasons": {ids[-1]: "完整流程包含所列步骤"} if grouped else {}}


async def _accept(host, job, root, state, ledger, values):
    for slot, value in zip(ledger["slots"], values, strict=True):
        slot["acceptance"] = await host._workflow_accept_slot(job, root, state, slot, value)
        slot["status"] = "valid"
    await host._workflow_batch_finished(job, root, state, ledger)


@pytest.mark.asyncio
async def test_complete_parent_last_groups_once_then_generates_metadata_once(tmp_path):
    host, job, state, records = await _entered(tmp_path, 17)
    ledger = host.submissions[-1]
    assert ledger["stage"] == PARTITION_STAGE and len(ledger["slots"]) == 1
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(ledger["slots"][0])])
    assert state["selection_cursor"] == 17 and state["workflow_group_count"] == 1
    ledger = host.submissions[-1]
    assert ledger["stage"] == METADATA_STAGE and len(ledger["slots"]) == 1
    assigned = _assignment(tmp_path, ledger["slots"][0]["context"])
    assert assigned["group"]["parent_record"] == records[-1]
    await _accept(host, job, tmp_path, state, ledger, [_candidate(assigned["manifest"])])
    assert state["phase"] == "skill_section" and len(host.submissions) == 2
    plan = _read(tmp_path / "plan.json")
    assert len(plan["skills"]) == 1
    name = plan["skills"][0]["name"]
    assert _read(tmp_path / "scopes" / (name + ".json"))["method_ordinals"] == list(range(17))
    assert plan["skills"][0]["source_refs"] == [row["item"]["source_files"][0] for row in records]
    for record in records:
        for leaf in record["leaf_items"]:
            assert _read(tmp_path / "method-decisions" / (leaf + ".json"))["name"] == name


@pytest.mark.asyncio
async def test_all_independent_methods_are_not_forced_under_eight_and_metadata_is_batched(tmp_path):
    host, job, state, _ = await _entered(tmp_path, 17)
    ledger = host.submissions[-1]
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(ledger["slots"][0], group=False)])
    assert state["workflow_group_count"] == 17
    widths = []
    while state["phase"] == METADATA_STAGE:
        ledger = host.submissions[-1]
        widths.append(len(ledger["slots"]))
        values = [_candidate(_assignment(tmp_path, slot["context"])["manifest"]) for slot in ledger["slots"]]
        await _accept(host, job, tmp_path, state, ledger, values)
    assert widths == [8, 8, 1]
    # Identical model slugs are not permission to merge independent groups.
    plan = _read(tmp_path / "plan.json")
    assert len({row["name"] for row in plan["skills"]}) == len(plan["skills"]) == 17


@pytest.mark.asyncio
async def test_capacity_repartitions_without_counting_dispositions_or_reprocessing_accepted_methods(tmp_path):
    host, job, state, _ = await _entered(tmp_path, 4)
    ledger = host.submissions[-1]
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(ledger["slots"][0], capacity=True)])
    assert state["selection_cursor"] == 0 and state["workflow_partition_count"] == 3
    ledger = host.submissions[-1]
    indexes = [_assignment(tmp_path, slot["context"])["catalog_indexes"] for slot in ledger["slots"]]
    assert indexes == [[0, 1], [2, 3]]
    await _accept(host, job, tmp_path, state, ledger, [_partition_value(slot) for slot in ledger["slots"]])
    assert state["selection_cursor"] == 4 and state["workflow_group_count"] == 2


@pytest.mark.asyncio
async def test_frozen_catalog_mutation_rejected_before_accepting_model_output(tmp_path):
    host, job, state, _ = await _entered(tmp_path)
    slot = host.submissions[-1]["slots"][0]
    path = tmp_path / "catalog/method/00000000.json"
    changed = _read(path)
    changed["item"]["boundary"] = "偷偷扩大范围"
    _save(path, changed)
    with pytest.raises(FrozenPlanningIntegrityError, match="catalog changed"):
        await host._workflow_validate_slot(tmp_path, state, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [False, True])
async def test_chinese_partition_reasons_are_validated_before_acceptance(tmp_path, capacity):
    host, job, state, _ = await _entered(tmp_path)
    slot = host.submissions[-1]["slots"][0]
    value = _partition_value(slot, capacity=capacity)
    if capacity:
        value["reason"] = "Complete workflow covers all assigned steps."
    else:
        value["reasons"][slot["packet"]["derived_controls"]["assigned_ids"][-1]] = "Complete workflow."
    with pytest.raises(ValueError, match="必须使用中文"):
        await host._workflow_accept_slot(job, tmp_path, state, slot, value)


@pytest.mark.asyncio
async def test_commit_replay_does_not_duplicate_groups_or_dispositions(tmp_path):
    host, job, state, _ = await _entered(tmp_path)
    ledger = host.submissions[-1]
    slot = ledger["slots"][0]
    slot.update(status="valid", acceptance=_partition_value(slot))
    before = copy.deepcopy(state)
    host._workflow_apply_batch(tmp_path, state, ledger)
    expected = copy.deepcopy(state)
    host._workflow_apply_batch(tmp_path, before, ledger)
    assert before == expected
    assert len(list((tmp_path / "workflow-planning/groups").glob("*.json"))) == 1


def test_revision_context_preserves_complete_feedback_questions_and_unknown_scope():
    feedback = "必须核实的不确定条件" * 1100
    questions = [{"qid": "q1", "question": "请保留原始限制" * 100}]
    actual = _feedback({"current_round": 2, "supplements": [{"feedback": feedback, "questions": questions}]})
    assert actual == _json({"feedback": feedback, "review_questions": questions,
                            "revision_context_is_evidence": False})


def test_second_round_feedback_can_change_metadata_without_old_authority_overwriting_it(tmp_path, monkeypatch):
    from team_miner import candidate_identity_orchestration as identities
    from team_miner.candidate_identity import IdentityCatalog, candidate_id

    candidate = _candidate({"assignment_sha256": "0" * 64})["candidate"]
    candidate["task"] = "依据已复核反馈重新核对请求并提供结论"
    candidate["source_refs"] = ["viking://current/original.md"]
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [candidate], "deferred": []})
    _save(tmp_path / "scopes" / (candidate["name"] + ".json"), {"ordinals": [0], "method_ordinals": [0]})
    _save(tmp_path / "state.json", {"workflow_partition_protocol": "te-workflow-partition.v2"})

    def forbidden_prior(*args):
        raise AssertionError("Previous round metadata must not silently replace current feedback revision")

    monkeypatch.setattr(identities, "_prior_authorities", forbidden_prior)
    job = {"job_id": "test", "current_round": 2, "staged_workflow_partition_protocol": "te-workflow-partition.v2"}
    identities._seed_catalog(job, tmp_path, "identity", include_reviews=False)
    db = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
    try:
        assert db.get(candidate_id("test", candidate["name"]))["candidate"] == candidate
    finally:
        db.close()
