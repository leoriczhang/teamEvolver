"""Identity control is global and bounded; grouping never rewrites factual business fields."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as orchestration
from team_miner.skill_miner.candidate_identity import (
    GROUP_SCHEMA,
    IDENTITY_PROTOCOL,
    IdentityCatalog,
    candidate_id,
    identity_brief,
    validate_identity_groups,
)
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.ov_staged_orchestration import Owner, drive, start


def candidate(name, task="独立退款方法", uri="viking://source/doc.md"):
    return {"name": name, "task": task, "trigger": "收到请求", "inputs": ["请求"],
            "outputs": ["结论"], "boundary": "按真实规则", "rationale": "可复用流程", "source_refs": [uri]}


def row(index, *, name=None, task=None, prior=False):
    result = candidate(name or f"candidate-{index}", task or f"独立退款方法 {index}")
    return {"candidate_id": candidate_id("job", result["name"]), "candidate": result, "ordinal": index,
            "prior": prior, "selected": True, "scope": {"ordinals": [index], "method_ordinals": [index]}}


def fixture(root, rows):
    _save(root / "plan.json", {"schema_version": planning.PLAN_SCHEMA,
                              "skills": [item["candidate"] for item in rows], "deferred": []})
    db = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    for item in rows:
        db.put(item)
        _save(root / "scopes" / (item["candidate"]["name"] + ".json"), item["scope"])
    db.close()
    _save(root / "identity" / "ready.json", {"count": len(rows), "protocol": IDENTITY_PROTOCOL})


def group_current(rows, child="group-1", *, after=1, ordinal=0):
    ids = [item["candidate_id"] for item in rows]
    return ({"child": child, "context": {"identity_ids": ids, "after": after,
                                         "identity_pass": 0, "anchor_ordinal": ordinal}},
            {"schema_version": GROUP_SCHEMA, "candidate_ids": list(ids),
             "groups": [{"candidate_ids": list(ids), "reason": "同一完整独立流程"}]})


@pytest.mark.parametrize("change", ["unknown", "missing", "duplicate", "metadata", "topfield"])
def test_groups_reject_unknown_missing_duplicates_and_metadata(change):
    rows = [row(0), row(1)]
    current, result = group_current(rows)
    if change == "unknown":
        result["groups"][0]["candidate_ids"][0] = "unknown"
    elif change == "missing":
        result["groups"][0]["candidate_ids"].pop()
    elif change == "duplicate":
        result["groups"].append(copy.deepcopy(result["groups"][0]))
    elif change == "metadata":
        result["groups"][0]["task"] = "改变业务流程"
    else:
        result["source_refs"] = []
    with pytest.raises(ValueError):
        validate_identity_groups(result, allowed_ids=current["context"]["identity_ids"])


def test_global_disk_retrieval_finds_older_identity_from_different_source(tmp_path):
    db = IdentityCatalog(tmp_path / "catalog.sqlite3")
    early = row(0, task="核对账单后形成退款结论")
    early["candidate"]["source_refs"] = ["viking://another-file/original.md"]
    db.put(early)
    for index in range(1, 1000):
        value = row(index, task=f"完全不同独立工作 {index}")
        value["candidate"]["trigger"] = f"other-{index}"
        value["candidate"]["source_refs"] = [f"viking://file/{index}.md"]
        db.put(value)
    last = row(1000, task="核对账单后形成退款结论")
    last["candidate"]["source_refs"] = ["viking://new-document/source.md"]
    db.put(last)
    assert db.related(last["candidate_id"], limit=1)[0]["candidate_id"] == early["candidate_id"]
    assert db.count() == 1001
    assert len(db.related(last["candidate_id"])) <= 7
    db.close()


def test_window_has_actual_budget_and_every_active_anchor_audited(tmp_path):
    rows = [row(index) for index in range(120)]
    for value in rows:
        value["candidate"]["inputs"] = ["入" * 240] * 16
        value["candidate"]["outputs"] = ["出" * 240] * 16
    fixture(tmp_path, rows)
    state = {"identity_pass": 0, "identity_cursor": 0, "language_policy": {"language": "zh-CN"}}
    calls = 0
    while True:
        window = orchestration._identity_window(tmp_path, state)
        if window is None:
            break
        assigned, context = window
        assert 1 < len(assigned) <= 8
        packet = planning.build_stage_packet("identity_group", "groups.json", controls={
            "identities": [identity_brief(value) for value in assigned],
            "required_candidate_ids": context["identity_ids"], "allowed_source_refs": [], "metadata_only": True,
        }, language_policy=state["language_policy"])
        assert len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) <= 32000
        current, result = group_current(assigned, f"window-{calls}")
        current["context"] = context
        result["groups"] = [{"candidate_ids": [value["candidate_id"]], "reason": "各自独立完整方法"}
                            for value in assigned]
        orchestration._accept_groups(tmp_path, current, result)
        state["identity_cursor"] = context["after"]
        calls += 1
    assert calls <= 120 * 2
    for index in range(120):
        assert (tmp_path / "identity" / "coverage" / f"0-{index:08d}.json").is_file()
    assert len(_read(tmp_path / "plan.json")["skills"]) == 120


def test_late_group_write_failure_replays_authoritative_survivor_and_preserves_aliases(tmp_path, monkeypatch):
    rows = [row(0), row(1, prior=True), row(2)]
    fixture(tmp_path, rows)
    current, result = group_current(rows[:2])
    original_save = orchestration._io()[2]
    def broken_save(path, value):
        if path == tmp_path / "plan.json":
            raise OSError("late disk failure")
        return original_save(path, value)
    original_io = orchestration._io
    monkeypatch.setattr(orchestration, "_io", lambda: (*original_io()[:2], broken_save, *original_io()[3:]))
    with pytest.raises(OSError, match="late"):
        orchestration._accept_groups(tmp_path, current, result)
    assert _read(tmp_path / "plan.json")["skills"][0]["name"] == "candidate-0"
    monkeypatch.setattr(orchestration, "_io", original_io)
    orchestration._accept_groups(tmp_path, current, result)
    plan = _read(tmp_path / "plan.json")
    assert [item["name"] for item in plan["skills"]] == ["candidate-1", "candidate-2"]
    assert plan["skills"][0]["task"] == rows[1]["candidate"]["task"]
    assert _read(tmp_path / "scopes" / "candidate-1.json")["ordinals"] == [0, 1]
    db = IdentityCatalog(tmp_path / "identity" / "catalog.sqlite3")
    assert not db.is_active(rows[0]["candidate_id"])
    assert db.related(rows[1]["candidate_id"], limit=7) == [rows[2]]
    db.close()
    assert _read(tmp_path / "identity" / "aliases.json")["candidate-0"]["name"] == "candidate-1"
    assert (tmp_path / "identity" / "journals" / "group-1.committed").exists()
    assert _read(tmp_path / "identity" / "ready.json")["count"] == 3


def test_alias_chain_name_and_id_remain_exact(tmp_path):
    rows = [row(0), row(1), row(2, prior=True)]
    fixture(tmp_path, rows)
    current, result = group_current(rows[:2])
    orchestration._accept_groups(tmp_path, current, result)
    current, result = group_current([rows[0], rows[2]], "group-2")
    orchestration._accept_groups(tmp_path, current, result)
    aliases = _read(tmp_path / "identity" / "aliases.json")
    for name in ("candidate-0", "candidate-1"):
        assert aliases[name]["name"] == "candidate-2"
        assert aliases[name]["candidate_id"] == rows[2]["candidate_id"]


def test_selection_probe_cannot_replace_legitimate_candidate_name(tmp_path):
    value = row(0, name="retrieval-probe", task="退款")
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA,
                                  "skills": [value["candidate"]], "deferred": []})
    _save(tmp_path / "scopes" / "retrieval-probe.json", value["scope"])
    controls = {"items": [], "allowed_source_refs": []}
    records = [{"item": {"task": "退款", "boundary": "按真实规则", "source_files": []}}]
    orchestration._selection_known({"job_id": "job", "current_round": 1}, tmp_path, records, controls)
    db = IdentityCatalog(tmp_path / "selection-identities" / "catalog.sqlite3")
    assert db.get(value["candidate_id"])["candidate"]["name"] == "retrieval-probe"
    assert db.is_active(value["candidate_id"])
    db.close()
    assert controls["known_candidates"][0]["name"] == "retrieval-probe"


def test_reuse_v2_copies_authority_without_string_repetition_or_business_rewrite():
    item = {"item_id": "method-1", "kind": "method", "task": "另一个表述", "boundary": "边界",
            "source_refs": ["unit-source"]}
    authority = candidate("canonical-identity")
    value = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": ["unit-1"],
             "skills": [{"derived_from": ["method-1"], "reuse_candidate_id": "known-1", "reason": "同一任务"}],
             "knowledge": [], "deferred": [], "resolved": []}
    result = planning.validate_selection(value, items=[item], input_ids=["unit-1"],
                                         known_candidates={"known-1": authority})
    assert result["skills"][0]["name"] == "canonical-identity"
    assert result["skills"][0]["source_refs"] == ["unit-source"]
    assert "reuse_candidate_id" not in result["skills"][0]
    assert authority["source_refs"] == ["viking://source/doc.md"]
    with pytest.raises(ValueError, match="known identity"):
        planning.validate_selection(value, items=[item], input_ids=["unit-1"], known_candidates={})
    changed = copy.deepcopy(value)
    changed["skills"][0]["boundary"] = "新规则"
    with pytest.raises(ValueError, match="rewrite"):
        planning.validate_selection(changed, items=[item], input_ids=["unit-1"],
                                    known_candidates={"known-1": authority})
    with pytest.raises(ValueError, match="exact"):
        planning.validate_selection(value, items=[item], input_ids=["unit-1"])


def test_legacy_frozen_writer_child_is_recovered_first_then_aux_and_valid_section_reused(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner, count=12)
        while (await owner._staged_load(job))[1]["phase"] != "skill_section":
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        # This emulates a genuinely old writer request already accepted remotely, with lost local ACK.
        state.pop("identity_review_complete", None)
        state.pop("identity_protocol", None)
        state["current"]["task_id"] = ""
        frozen = {key: (root / state["current"][key]).read_bytes()
                  for key in ("request_file", "compiler_file", "packet_file")}
        frozen_child = state["current"]["child"]
        _save(root / "state.json", state)
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == calls
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        job = await drive(owner, job)
        assert job["status"] == "waiting_human"
        _, after = await owner._staged_load(job)
        assert after["writer_resume"]["section_count"] == 1
        assert after["writer_resume"]["cursor"] >= 1
        assert (root / "sections" / "refund-review" / "00000000.json").exists()
        assert (root / "children" / frozen_child / "result.txt").exists()
        for key, content in frozen.items():
            assert (root / state["current"][key]).read_bytes() == content
        assert (owner.repo.workspace(job) / "semantic_reports/candidate-identities.md").is_file()
    asyncio.run(check())


@pytest.mark.parametrize("same_scope,opposite_order", [(False, False), (False, True), (True, True)])
def test_reviewed_method_identity_rebuilds_shared_gap_without_erasing_other_method(
    tmp_path, same_scope, opposite_order,
):
    refs = ["viking://doc/a.md", "viking://doc/b.md"]
    other_refs = refs if same_scope else ["viking://doc/c.md", "viking://doc/d.md"]
    gap = {"reason": "暂缺条件", "source_refs": refs}
    other_gap = {"reason": "暂缺条件", "source_refs": other_refs}
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [],
                                  "deferred": [gap] if same_scope else [gap, other_gap]})
    for index, outcome in enumerate(("candidate", "deferred")):
        original_gap = copy.deepcopy(gap if index == 0 else other_gap)
        if opposite_order:
            original_gap["source_refs"].reverse()
        value = candidate("restored-method", uri=refs[0]) if outcome == "candidate" else None
        if value:
            value["source_refs"] = refs
        _save(tmp_path / "method-decisions" / f"leaf-{index}.json", {
            "target": "deferred", "name": "", "reason": "暂缺条件",
        })
        _save(tmp_path / "evidence-review" / "results" / f"{index:08d}.json", {
            "proposal_index": index, "item_id": f"method-{index}", "leaf_items": [f"leaf-{index}"],
            "outcome": outcome, "candidate": value, "reason": "原文支持完整方法" if value else "缺真实附件",
            "source_refs": refs if value else other_refs, "previous_gap": original_gap,
            "ordinals": [0, 1], "method_ordinals": [1],
        })
    _save(tmp_path / "evidence-review" / "manifest.json", {"proposal_indexes": [0, 1]})
    orchestration._seed_catalog({"job_id": "job", "current_round": 1}, tmp_path, "identity", include_reviews=True)
    plan = _read(tmp_path / "plan.json")
    assert [item["name"] for item in plan["skills"]] == ["restored-method"]
    assert plan["deferred"] == [{"reason": "缺真实附件", "source_refs": other_refs}]
    assert _read(tmp_path / "method-decisions/leaf-0.json")["target"] == "candidate"
    assert _read(tmp_path / "method-decisions/leaf-1.json") == {
        "target": "deferred", "name": "", "reason": "缺真实附件",
    }
    assert _read(tmp_path / "identity/original-decisions/leaf-1.json")["reason"] == "暂缺条件"
    assert _read(tmp_path / "identity/original-plan.json")["deferred"][0] == gap


def test_partial_review_ledger_cannot_be_adopted(tmp_path):
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []})
    _save(tmp_path / "evidence-review/manifest.json", {"proposal_indexes": [0, 1]})
    _save(tmp_path / "evidence-review/results/00000000.json", {"proposal_index": 0})
    with pytest.raises(ValueError, match="Every deferred method"):
        orchestration._seed_catalog({"job_id": "job", "current_round": 1}, tmp_path, "identity", include_reviews=True)
    assert not (tmp_path / "identity/ready.json").exists()


def test_v2_cannot_redefine_known_same_name_boundary():
    value = candidate("previous-identity")
    changed = {key: copy.deepcopy(item) for key, item in value.items() if key != "source_refs"}
    changed.update(boundary="新的独立业务边界", derived_from=["method-1"])
    output = {"schema_version": planning.SELECTION_SCHEMA_V2, "input_ids": ["unit-1"], "skills": [changed],
              "knowledge": [], "deferred": [], "resolved": []}
    items = [{"item_id": "method-1", "kind": "method", "task": "任务", "boundary": "边界",
              "source_refs": ["unit-source"]}]
    with pytest.raises(ValueError, match="canonical workflow"):
        planning.validate_selection(output, items=items, input_ids=["unit-1"], known_candidates={"known-1": value})


def test_two_round_renamed_duplicate_reuses_prior_survivor_and_exact_twelve_case_ids(tmp_path):
    async def check():
        from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
        from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
        owner = Owner(tmp_path)
        job, entries = owner.new()
        first_manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        first_manifest.write_text(json.dumps(entries), encoding="utf-8")
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(first_manifest))
        language = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, language, ""))
        dataset = owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json"
        document = json.loads(dataset.read_text())
        case_ids = [f"reviewed-custom:{index}" for index in range(12)]
        for case, identity in zip(document["cases"], case_ids, strict=True):
            case["case_id"] = identity
        dataset.write_text(json.dumps(document), encoding="utf-8")
        job = owner.repo.prepare_next_round(job["job_id"], job["pending_checkpoint"]["id"], {}, [], {})
        entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
        job_dir = owner.repo._job_dir(job["job_id"])
        job = owner.repo.update_job(job["job_id"], input_manifest=str(job_dir / "round_inputs/round-2/manifest.json"))
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        original_request = owner.ov.request
        async def renamed_request(account, method, path, **kwargs):
            accepted = await original_request(account, method, path, **kwargs)
            if method == "POST" and path == "/api/v1/compile":
                request = kwargs["json"]
                packet = json.loads(owner.ov.contents[request["from"][0] + "/packet.json"])
                output = request["to"] + "/" + packet["output_path"]
                if packet["stage"] == "plan":
                    value = json.loads(owner.ov.contents[output])
                    value["skills"][0]["name"] = "same-workflow-another-name"
                    owner.ov.contents[output] = json.dumps(value)
                elif packet["stage"] == "identity_group":
                    ids = packet["derived_controls"]["required_candidate_ids"]
                    owner.ov.contents[output] = json.dumps({
                        "schema_version": GROUP_SCHEMA, "candidate_ids": ids,
                        "groups": [{"candidate_ids": ids, "reason": "相同完整工作流，沿用既有身份"}],
                    })
            return accepted
        owner.ov.request = renamed_request
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, language, ""))
        assert job["status"] == "waiting_human"
        assert [case["case_id"] for case in json.loads(dataset.read_text())["cases"]] == case_ids
        root, state = await owner._staged_load(job)
        assert [row["name"] for row in _read(root / "plan.json")["skills"]] == ["refund-review"]
        aliases = _read(root / "identity/aliases.json")
        assert aliases["same-workflow-another-name"]["name"] == "refund-review"
        packets = [_read(path) for path in root.glob("children/*/packet.json")]
        assert len([packet for packet in packets if packet["stage"] == "case"]) == 12
        assert len([packet for packet in packets if packet["stage"] == "identity_group"]) <= 2
        assert state["identity_review_complete"]
    asyncio.run(check())
