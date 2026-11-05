"""Generated role-name omissions are source-bound projections, never rewrites of Native evidence."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.case_corrections import corrected_case, omit_foreign_case_scope, omit_unsupported_query_name
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha
from tests.miner.skill_miner.fixtures import build_document


def fixture(root):
    chunk = planning.source_chunks("客户别名zhanglei，未命名的规划师询问客户已准备的费用。", "viking://original/dialogue.md")[0]
    chunk["ordinal"] = 0
    _save(root / "chunks/00000000.json", chunk)
    _save(root / "chunk-ids" / (chunk["id"] + ".json"), {"ordinal": 0})
    value = build_document("collect-finance", [{"query": "你是规划师张磊，请收集客户张先生的财务信息。"}])["cases"][0]
    value["case_id"] = "collect-finance-case-01"
    value["provenance"]["session_ids"] = [chunk["source_uri"]]
    _save(root / "cases/collect-finance/00.json", value)
    context = {"case_id": value["case_id"], "input_ids": [chunk["id"]], "source_refs": [chunk["source_uri"]]}
    _save(root / "case-batches/collect-finance/outcomes/accepted-00.json",
          {"status": "valid", "context": context, "case": value})
    return value, chunk


def correct(root, value):
    return omit_unsupported_query_name(
        root, "collect-finance", 0, expected_case_sha256=_sha(_json(value)),
        start=value["query"].index("张磊"), text="张磊",
        explanation="已核对当前原文：客户别名不能变成未命名规划师的姓名，保持角色称谓。",
    )


def test_omission_keeps_original_case_outcome_sources_and_all_other_fields(tmp_path):
    value, chunk = fixture(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    receipt = correct(tmp_path, value)
    projected = corrected_case(tmp_path, "collect-finance", 0)
    assert projected["query"] == "你是规划师，请收集客户张先生的财务信息。"
    assert {k: v for k, v in projected.items() if k != "query"} == {k: v for k, v in value.items() if k != "query"}
    assert all(p.read_bytes() == text for p, text in before.items())
    assert receipt["source_fingerprints"] == [[chunk["id"], chunk["sha256"]]]
    assert receipt["unknown_facts_resolved"] is False
    with pytest.raises(ValueError, match="overwrite"):
        correct(tmp_path, value)


@pytest.mark.parametrize("tamper", ["case", "source", "context", "span"])
def test_correction_rejects_changed_frozen_dependencies_or_projection(tmp_path, tamper):
    value, chunk = fixture(tmp_path)
    correct(tmp_path, value)
    if tamper == "case":
        changed = copy.deepcopy(value)
        changed["query"] += "更改事实"
        _save(tmp_path / "cases/collect-finance/00.json", changed)
    elif tamper == "source":
        chunk["content"] += "更改原文"
        _save(tmp_path / "chunks/00000000.json", chunk)
    else:
        path = tmp_path / "case-corrections/collect-finance/00.json"
        record = _read(path)
        if tamper == "context":
            record["accepted_context"]["input_ids"] = ["foreign"]
        else:
            record["omission"]["start"] += 1
        _save(path, record)
    with pytest.raises(ValueError):
        corrected_case(tmp_path, "collect-finance", 0)


def test_source_backed_name_cannot_be_removed_by_unsupported_identity_repair(tmp_path):
    value, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="source-backed"):
        omit_unsupported_query_name(tmp_path, "collect-finance", 0, expected_case_sha256=_sha(_json(value)),
                                    start=value["query"].index("规划师"), text="规划师",
                                    explanation="不得删除已有来源称谓")
    assert not (tmp_path / "case-corrections").exists()


def scope_fixture(root):
    value, primary = fixture(root)
    secondary = planning.source_chunks("另一个客户家庭的排序：子女教育第一，医疗第二。", "viking://original/other.md")[0]
    secondary["ordinal"] = 1
    _save(root / "chunks/00000001.json", secondary)
    _save(root / "chunk-ids" / (secondary["id"] + ".json"), {"ordinal": 1})
    value["query"] = "请与退休客户沟通。\n1. 分析医疗费用。\n2. 子女教育第一，医疗第二。\n3. 评估已有保额。"
    value["checks"] += [{"id": "foreign-order", "kind": "output", "text": "子女教育第一，医疗第二。"}]
    value["checks"][-2]["text"] += "随后排序。"
    value["provenance"]["session_ids"].append(secondary["source_uri"])
    _save(root / "cases/collect-finance/00.json", value)
    context = {"case_id": value["case_id"], "input_ids": [primary["id"], secondary["id"]],
               "method_input_ids": [primary["id"]], "context_input_ids": [secondary["id"]],
               "source_refs": value["provenance"]["session_ids"]}
    _save(root / "case-batches/collect-finance/outcomes/accepted-00.json",
          {"status": "valid", "context": context, "case": value})
    text = "2. 子女教育第一，医疗第二。\n"
    trajectory = value["checks"][-2]
    kwargs = {
        "expected_case_sha256": _sha(_json(value)),
        "foreign_context_claim": {"input_id": secondary["id"], "quote": "子女教育第一，医疗第二。"},
        "query_omissions": [{"start": value["query"].index(text), "text": text}],
        "omitted_checks": [value["checks"][-1]],
        "check_text_omissions": [{"id": trajectory["id"], "omissions": [
            {"start": trajectory["text"].index("随后排序。"), "text": "随后排序。"},
        ]}],
        "explanation": "人工对照两个原始情境：排序来自另一个客户，不能套到退休客户；仅删除越界内容。",
    }
    return value, primary, kwargs


def test_inspected_scope_pruning_keeps_base_roles_sources_and_supported_checks(tmp_path):
    value, _, kwargs = scope_fixture(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    record = omit_foreign_case_scope(tmp_path, "collect-finance", 0, **kwargs)
    result = corrected_case(tmp_path, "collect-finance", 0)
    assert result["query"] == "请与退休客户沟通。\n1. 分析医疗费用。\n2. 评估已有保额。"
    assert len([c for c in result["checks"] if c["kind"] == "output"]) == 12
    assert result["checks"][:-1] == value["checks"][:-2]
    assert result["provenance"] == value["provenance"]
    assert all(path.read_bytes() == original for path, original in before.items())
    assert record["unknown_facts_resolved"] is False and record["business_quality_passed"] is False
    with pytest.raises(ValueError, match="overwrite"):
        omit_foreign_case_scope(tmp_path, "collect-finance", 0, **kwargs)


@pytest.mark.parametrize("fault", ["method-claim", "unsupported-quote", "overlap", "changed-check", "too-few-checks"])
def test_scope_pruning_does_not_weaken_original_roles_shape_or_exact_edit_checks(tmp_path, fault):
    value, primary, kwargs = scope_fixture(tmp_path)
    if fault == "method-claim":
        kwargs["foreign_context_claim"]["input_id"] = primary["id"]
    elif fault == "unsupported-quote":
        kwargs["foreign_context_claim"]["quote"] = "原文不存在的分类结论"
    elif fault == "overlap":
        kwargs["query_omissions"] *= 2
    elif fault == "changed-check":
        kwargs["omitted_checks"][0] = {**kwargs["omitted_checks"][0], "text": "篡改检查项"}
    else:
        kwargs["omitted_checks"].append(value["checks"][0])
    with pytest.raises(ValueError):
        omit_foreign_case_scope(tmp_path, "collect-finance", 0, **kwargs)
    assert not (tmp_path / "case-corrections").exists()


def test_scope_export_revalidates_original_context_and_projection(tmp_path):
    _, _, kwargs = scope_fixture(tmp_path)
    omit_foreign_case_scope(tmp_path, "collect-finance", 0, **kwargs)
    path = tmp_path / "case-corrections/collect-finance/00.json"
    record = _read(path)
    record["query_omissions"][0]["start"] += 1
    _save(path, record)
    with pytest.raises(ValueError):
        corrected_case(tmp_path, "collect-finance", 0)


def test_final_dataset_uses_corrected_projection_and_retains_twelve_original_case_records(tmp_path):
    from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchOwner
    from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start

    async def run():
        owner = BatchOwner(tmp_path)
        request, assemble = owner.ov.request, owner._staged_assemble

        async def wrong_executor_name(account, method, path, **kwargs):
            result = await request(account, method, path, **kwargs)
            body = kwargs.get("json", {})
            if method == "POST" and path == "/api/v1/compile" and "/stages/case_batch-" in body["from"][0]:
                for uri in list(owner.ov.contents):
                    if not uri.startswith(body["to"] + "/"):
                        continue
                    value = json.loads(owner.ov.contents[uri])
                    if value.get("case_id", "").endswith("case-01"):
                        value["query"] = "你是规划师张磊。" + value["query"]
                        owner.ov.contents[uri] = _json(value)
            return result

        async def repair_before_assembly(job, root, state):
            value = _read(root / "cases/refund-review/00.json")
            omit_unsupported_query_name(root, "refund-review", 0, expected_case_sha256=_sha(_json(value)),
                                        start=value["query"].index("张磊"), text="张磊",
                                        explanation="修复模拟的客户姓名被当成执行者姓名的问题，保留未知身份。")
            return await assemble(job, root, state)

        owner.ov.request = wrong_executor_name
        owner._staged_assemble = repair_before_assembly
        final = await drive(owner, await start(owner, count=12))
        assert final["status"] == "waiting_human", final.get("error")
        root, _ = await owner._staged_load(final)
        raw = _read(root / "cases/refund-review/00.json")
        dataset = _read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")
        assert len(dataset["cases"]) == 12 and len(list((root / "cases/refund-review").glob("*.json"))) == 12
        assert raw["query"].startswith("你是规划师张磊。")
        assert dataset["cases"][0]["query"].startswith("你是规划师。")
        assert dataset["cases"][0]["checks"] == raw["checks"]

    asyncio.run(run())
