"""Case narratives stay within source scope while review requirements and frozen requests survive."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.case_context import CASE_NARRATIVE_PROTOCOL, case_history_controls
from team_miner.skill_miner.compile_strategy import render_case_batch_compiler
from team_miner.skill_miner.ov_case_batch_orchestration import _bounded_case_revision
from team_miner.skill_miner.ov_case_group_orchestration import render_case_group_compiler
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchOwner, to_batch


def test_history_uses_full_query_fingerprints_without_leaking_other_scenario_facts():
    cases = [{"case_id": "method-case-01", "query": "规划师甲帮助客户乙，金额八万元。" + "后续条件" * 100}]
    original = copy.deepcopy(cases)
    controls = case_history_controls(cases)
    assert controls["case_narrative_protocol"] == CASE_NARRATIVE_PROTOCOL
    assert controls["previous_queries"] == []
    assert controls["previous_query_fingerprints"] == [{
        "case_id": cases[0]["case_id"],
        "query_sha256": hashlib.sha256(cases[0]["query"].encode()).hexdigest(),
    }]
    assert "客户乙" not in _json(controls) and "八万元" not in _json(controls)
    assert cases == original
    assert controls["scenario_binding_policy"] == "te-case-scenario-binding.v1"
    assert "never merge customer facts" in controls["case_narrative_rule"]
    assert "adds no goal outside" in controls["case_narrative_rule"]


def test_new_case_compilers_make_coverage_a_preference_without_weakening_source_or_shape_checks():
    for renderer in (render_case_batch_compiler, render_case_group_compiler):
        old = renderer(SKILL_BODY)
        new = renderer(SKILL_BODY, source_bound_context=True)
        assert old != new and CASE_NARRATIVE_PROTOCOL not in old
        assert "Do not defer merely because the preferred dimension is absent" in new
        assert "never invent conditions or pad checks" in new
        assert "12-24 distinct source-grounded output checks and 1-8 trajectory checks" in new
        assert "EXCLUSIVE Case goal" in new
        assert "Whole previous queries stay local" in new
        assert renderer(SKILL_BODY, source_bound_context=False) == old


def test_case_revision_omits_prior_body_but_preserves_review_and_source(tmp_path, monkeypatch):
    from team_miner.skill_miner import ov_staged_orchestration as host

    prior = {"name": "method", "case_ids": [f"method-case-{i:02d}" for i in range(1, 13)],
             "skill_body_sha256": "body-digest", "skill_body_excerpt": "旧正文中的另一个客户、规划师和金额"}
    monkeypatch.setattr(host, "_prior_candidate_rows", lambda *a, **kw: iter([copy.deepcopy(prior)]))
    review = {"feedback": "不能把原材料问题当作模型新错误", "review_questions": [{
        "qid": "unknown-account", "question": "两个账户体系如何映射？尚未确认。",
    }]}
    (tmp_path / "revision.txt").write_text(_json(review))
    chunks = planning.source_chunks("当前场景：规划师小瑞和客户欣然。", "viking://original/current.md")
    context = {"skill_name": "method", "case_id": "method-case-05"}
    packet = planning.build_stage_packet("case", "case-output-04.json", evidence_chunks=chunks,
                                         controls=case_history_controls([]))
    result = _bounded_case_revision(tmp_path, packet, context, "compiler", "instruction")
    note = json.loads(result["revision_context"])
    assert "skill_body_excerpt" not in note["previous_candidate"]
    assert note["previous_candidate"]["skill_body_excerpt_omitted"]
    assert note["previous_candidate"]["skill_body_sha256"] == "body-digest"
    assert note["review_questions"][0]["question"] == review["review_questions"][0]["question"]
    assert note["feedback"] == review["feedback"]
    assert result["evidence"] == chunks
    assert "另一个客户" not in result["revision_context"]
    assert (tmp_path / "revision.txt").read_text() == _json(review)

    # Non-Case writers retain the prior method body they need for incremental integration.
    artifact = planning.build_stage_packet("skill_section", "body.json", evidence_chunks=chunks)
    artifact = _bounded_case_revision(tmp_path, artifact, context, "compiler", "instruction")
    assert json.loads(artifact["revision_context"])["previous_candidate"]["skill_body_excerpt"] == prior[
        "skill_body_excerpt"
    ]


def test_accepted_case_request_resumes_without_rebuilding_frozen_context(tmp_path, monkeypatch):
    async def run():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        paths = [root / current[key] for key in ("packet_file", "request_file", "compiler_file")]
        paths += [root / row["local_path"] for row in _read(root / current["packet_file"])]
        before = {path: path.read_bytes() for path in paths}
        accepted_count = len(owner.ov.accepted)

        def must_not_rebuild(*args, **kwargs):
            raise AssertionError("An accepted Native request must use its frozen packet")

        monkeypatch.setattr(
            "team_miner.skill_miner.ov_case_batch_orchestration.case_history_controls", must_not_rebuild)
        resumed = await owner._resume_staged_round(job)
        assert resumed["ov_task_id"] == current["task_id"]
        assert len(owner.ov.accepted) == accepted_count
        assert all(path.read_bytes() == content for path, content in before.items())

    asyncio.run(run())


def test_inspected_coverage_recovery_keeps_siblings_requests_and_format_counts(tmp_path, monkeypatch):
    from team_miner.skill_miner import case_recovery
    from team_miner.skill_miner.ov_staged_orchestration import _save, _sha
    from tests.miner.skill_miner.fixtures import build_document
    from tests.miner.skill_miner.support.candidate_identity import candidate

    root, workspace = tmp_path / "staged", tmp_path / "workspace"
    method = candidate("refund-review")
    name = method["name"]
    value = build_document(name, [{"query": "按已提供的原文执行完整退款审核流程。"}])["cases"][0]
    value["case_id"] = name + "-case-01"
    value["provenance"]["session_ids"] = ["viking://source/original.md"]
    deferred = {"reason": "Preferred exception absent; ordinary method supported",
                "source_refs": value["provenance"]["session_ids"]}
    plan = {"skills": [candidate("other-method")], "deferred": [deferred]}
    state = {"phase": "case", "candidate_index": 0, "candidate_names": ["other-method"],
             "current": {"task_id": "accepted-native", "context": {"skill_name": "other-method"}}}
    _save(root / "identity/original-plan.json", {"skills": [method]})
    _save(root / "state.json", state)
    _save(root / "plan.json", plan)
    _save(root / f"deferred-candidates/{name}.json", deferred)
    _save(root / f"scopes/{name}.json", {"method_lineage_verified": True})
    slots = [{"index": i, "case_id": f"{name}-case-{i + 1:02d}",
              "status": "valid" if i == 0 else "pending", "group_attempt": 8, "format_attempts": 1}
             for i in range(12)]
    _save(root / f"case-batches/{name}/ledger.json", {"case_ids": [s["case_id"] for s in slots], "slots": slots})
    _save(root / f"cases/{name}/00.json", value)
    _save(root / "children/old-child/metadata.json", {"task_id": "old-completed-native"})
    for slot in slots[1:]:
        _save(root / f"case-batches/{name}/outcomes/old-child-{slot['index']:02d}.json",
              {"status": "deferred", "context": {"case_group_count": 8}})
    request = root / "children/current/request.json"
    _save(request, {"accepted": "unchanged"})
    previous_case, previous_request = (root / f"cases/{name}/00.json").read_bytes(), request.read_bytes()
    monkeypatch.setattr(case_recovery, "_restore_texts", lambda *args: ("原先完整方法正文", "原先评测说明"))
    kwargs = {"expected_plan_sha256": _sha(_json(plan)), "expected_deferred_sha256": _sha(_json(deferred)),
              "explanation": "已对照原文确认，缺失的是覆盖偏好，已有完整方法支持。"}
    with pytest.raises(ValueError, match="frozen selected identity"):
        case_recovery.reopen_case_candidate(root, workspace, {**method, "task": "伪造方法"}, **kwargs)
    receipt = case_recovery.reopen_case_candidate(root, workspace, method, **kwargs)
    assert receipt["original_state"] == state and receipt["original_case_ledger"]["slots"] == slots
    assert (root / f"cases/{name}/00.json").read_bytes() == previous_case and request.read_bytes() == previous_request
    ledger = _read(root / f"case-batches/{name}/ledger.json")
    assert all(slot["format_attempts"] == 1 for slot in ledger["slots"])
    assert ledger["slots"][0] == slots[0] and ledger["slots"][1]["group_attempt"] == 0
    current = _read(root / "state.json")
    assert current["current"] == state["current"] and current["candidate_index"] == state["candidate_index"]
    assert current["candidate_names"] == ["other-method", name]
    with pytest.raises(ValueError, match="allowed once"):
        case_recovery.reopen_case_candidate(root, workspace, method, **kwargs)
