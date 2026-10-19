"""Auxiliary Case repair keeps checkpoints/history while rejecting source, Native and scope drift."""

from __future__ import annotations

import copy

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.case_corrections import corrected_case
from team_miner.case_revisions import PROTOCOL, register_source_revision
from team_miner.compile_strategy import render_case_batch_compiler
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _json, _package, _read, _save, _sha
from team_miner.tests.dataset_fixtures import build_document
from team_miner.tests.test_candidate_identity import candidate
from team_miner.tests.test_case_corrections import fixture


def revision_fixture(tmp_path):
    root = tmp_path / "job-1/round_inputs/round-2/staged"
    original, primary = fixture(root)
    name = "collect-finance"
    method = candidate(name)
    _save(root / "plan.json", {"skills": [method]})
    neighbor = planning.source_chunks("同一客户补充原文：已备保障情况及费用讨论。", primary["source_uri"])[0]
    neighbor["ordinal"] = 1
    _save(root / "chunks/00000001.json", neighbor)
    _save(root / "chunk-ids" / (neighbor["id"] + ".json"), {"ordinal": 1})
    _save(root / "scopes" / (name + ".json"), {"ordinals": [0, 1], "method_ordinals": [0]})
    policy = {"language": "zh-CN"}
    compiler = render_case_batch_compiler(SKILL_BODY, source_bound_context=True)
    (root / "aux-compilers").mkdir()
    (root / "aux-compilers/case_batch_source_bound.md").write_text(compiler)
    compiler_name, published = _package(compiler)
    _save(root / "state.json", {"language_policy": policy,
                               "aux_compilers": {"case_batch_source_bound": _sha(compiler)}})
    controls = {"candidate": {key: method[key] for key in ("name", "task", "boundary", "trigger")},
                "skill_name": name, "case_id": original["case_id"], "input_ids": [primary["id"], neighbor["id"]],
                "method_input_ids": [primary["id"]], "context_input_ids": [neighbor["id"]],
                "allowed_source_refs": [primary["source_uri"]], "case_revision_protocol": PROTOCOL}
    packet = planning.build_stage_packet("case", "case-output-00.json", evidence_chunks=[primary, neighbor],
                                         controls=controls, language_policy=policy)
    uri = "viking://resources/agent_skill_workspace/runs/job-1/rounds/round-2/stages/review-case-fixture"
    request = {"from": [uri + "/input"], "to": uri + "/output", "skill": "viking://agent/skills/" + compiler_name}
    revised = build_document(name, [{"query": "请根据当前原文核对保障与费用，保留未知事实。",
                                    "requirements": [f"核对已提供的来源条件{n}" for n in range(12)],
                                    "trajectory_requirements": ["先核对来源再完成分析。"]}])["cases"][0]
    revised["case_id"] = original["case_id"]
    revised["provenance"]["session_ids"] = [primary["source_uri"]]
    raw = _json(revised)
    manifest = {"protocol": PROTOCOL, "name": name, "index": 0, "original_case_sha256": _sha(_json(original)),
                "packet_sha256": _sha(_json(packet)), "compiler_sha256": _sha(published),
                "request_sha256": _sha(_json(request)), "native_result_sha256": _sha(raw), "native_task_id": "cmp_test",
                "input_uri": uri + "/input", "output_uri": uri + "/output", "input_ids": controls["input_ids"],
                "method_input_ids": controls["method_input_ids"], "context_input_ids": controls["context_input_ids"],
                "source_refs": controls["allowed_source_refs"], "revision_case_sha256": _sha(raw),
                "source_fingerprints": [[c["id"], c["sha256"]] for c in [primary, neighbor]]}
    folder = root / "case-revisions" / name / "00"
    for file, value in {"completed-manifest.json": manifest, "packet.json": packet, "request.json": request,
                        "accepted.json": {"task_id": "cmp_test"}, "task.json": {
                            "task_id": "cmp_test", "status": "completed", "meta": {"request": request},
                            "result": {"created": [uri + "/output/case-output-00.json"], "conflicts": []},
                        }}.items():
        _save(folder / file, value)
        if file == "packet.json":
            (folder / file).write_text(_json(value))
    (folder / "compiler.md").write_text(published)
    (folder / "raw-output.json").write_text(raw)
    return root, folder, original, revised


def test_source_revision_changes_only_projection_and_retains_original_case_native_and_checkpoint(tmp_path):
    root, folder, original, revised = revision_fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    record = register_source_revision(root, "collect-finance", 0, explanation="对照原文修订越界目标，不填新事实。")
    assert corrected_case(root, "collect-finance", 0) == revised
    assert _read(root / "cases/collect-finance/00.json") == original
    assert all(p.read_bytes() == content for p, content in before.items())
    assert record["business_quality_passed"] is False and record["unknown_facts_resolved"] is False
    with pytest.raises(ValueError, match="once-only"):
        register_source_revision(root, "collect-finance", 0, explanation="不能覆盖已有修订。")


@pytest.mark.parametrize("fault", ["source", "scope", "candidate", "compiler", "native", "output", "record"])
def test_source_revision_fails_closed_on_frozen_dependency_tampering(tmp_path, fault):
    root, folder, _, _ = revision_fixture(tmp_path)
    register_source_revision(root, "collect-finance", 0, explanation="已核对的业务修订。")
    if fault == "source":
        path = root / "chunks/00000001.json"
        changed = _read(path)
        changed["content"] += "篡改来源"
    elif fault == "scope":
        path = root / "scopes/collect-finance.json"
        changed = {"ordinals": [0], "method_ordinals": [0]}
    elif fault == "candidate":
        path = root / "plan.json"
        changed = _read(path)
        changed["skills"][0]["task"] += "另一目标"
    elif fault == "compiler":
        (folder / "compiler.md").write_text("未经受理的编译器")
        changed = None
    elif fault in {"native", "output"}:
        path = folder / "task.json"
        changed = _read(path)
        if fault == "native":
            changed["task_id"] = "foreign-native"
        else:
            changed["result"]["created"].append("viking://foreign/output.json")
    else:
        path = root / "case-corrections/collect-finance/00.json"
        changed = _read(path)
        changed["original_case_sha256"] = "foreign-case-sha"
    if changed is not None:
        _save(path, changed)
    with pytest.raises(ValueError):
        corrected_case(root, "collect-finance", 0)


def test_source_revision_refuses_english_display_and_insufficient_checklist(tmp_path):
    root, folder, _, revised = revision_fixture(tmp_path)
    for mutate in [lambda v: v.update(query="English task description"), lambda v: v["checks"].pop(0)]:
        value = copy.deepcopy(revised)
        mutate(value)
        raw = _json(value)
        manifest = _read(folder / "completed-manifest.json")
        manifest.update(native_result_sha256=_sha(raw), revision_case_sha256=_sha(raw))
        _save(folder / "completed-manifest.json", manifest)
        (folder / "raw-output.json").write_text(raw)
        with pytest.raises(ValueError):
            register_source_revision(root, "collect-finance", 0, explanation="不放宽格式及中文检查。")
    assert not (root / "case-corrections").exists()
