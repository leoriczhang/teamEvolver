"""Pure TE invariants for staging, full provenance and deterministic assembly."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner.bounded_compile_plan import (
    assemble_dataset,
    build_stage_packet,
    compose_skill,
    is_deferred,
    iter_skill_chunks,
    planning_items,
    selection_plan,
    source_chunks,
    validate_chunks,
    validate_deferred,
    validate_index,
    validate_merge,
    validate_plan,
    validate_section,
    validate_section_skip,
    validate_selection,
)
from team_skills.library.frontmatter import parse_skill_md_text, render_skill_md, validate_skill_md
from tests.miner.skill_miner.fixtures import build_document

URI = "viking://resources/team/source/退货.md"


def candidate(refs):
    return {
        "name": "return-check",
        "task": "核验退货申请",
        "trigger": "收到申请",
        "inputs": ["申请及原始条款"],
        "outputs": ["核验结果和缺口"],
        "boundary": "仅根据提供的条款",
        "rationale": "同一独立任务",
        "source_refs": refs,
    }


def test_chunks_are_lossless_stable_and_bound_actual_json_escaping():
    text = ('\\"\n中文\t' * 3000) + "9000 ≤ 12000"
    chunks = source_chunks(text, URI, max_chars=1700)

    assert "".join(chunk["content"] for chunk in chunks) == text
    assert chunks == source_chunks(text, URI, max_chars=1700)
    assert chunks[0]["start_char"] == 0 and chunks[-1]["end_char"] == len(text)
    assert all(left["end_char"] == right["start_char"] for left, right in zip(chunks, chunks[1:]))
    assert all(len(json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))) <= 1700 for chunk in chunks)
    for chunk in chunks:
        assert chunk["sha256"] == hashlib.sha256(chunk["content"].encode()).hexdigest()
        assert chunk["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert chunk["source_uri"] == URI
    assert source_chunks(text, URI + "-other", max_chars=1700)[0]["id"] != chunks[0]["id"]


def test_packet_snapshot_cannot_drift_when_controls_or_original_chunk_metadata_change():
    chunks = source_chunks("9000 ≤ 12000", URI)
    controls = {"candidate": candidate([URI])}
    packet = build_stage_packet("skill_section", "section.json", evidence_chunks=chunks, controls=controls)
    before = copy.deepcopy(packet)
    chunks[0]["content"] = "changed"
    controls["candidate"]["boundary"] = "changed"

    assert packet == before
    assert packet["derived_controls_are_evidence"] is False
    assert packet["revision_context_is_evidence"] is False
    with pytest.raises(ValueError, match="bounded input"):
        build_stage_packet("case", "case.json", controls={"note": "x" * 33000})


@pytest.mark.parametrize("field,value", [("id", "forged"), ("sha256", "0" * 64), ("start_char", "0")])
def test_forged_chunk_identity_hash_or_offsets_fail_before_model_submission(field, value):
    chunk = source_chunks("完整原文", URI)[0]
    chunk[field] = value
    with pytest.raises(ValueError):
        validate_chunks([chunk])


def test_index_cannot_claim_full_input_ids_while_silently_ignoring_a_source():
    aliases = ["unit-first", "unit-second"]
    index = {
        "schema_version": "skill-miner.index.v1",
        "input_ids": aliases,
        "methods": [],
        "facts": [],
        "deferred": [{"reason": "仅有事实", "source_refs": aliases[:1]}],
    }
    with pytest.raises(ValueError, match="Every assigned source"):
        validate_index(index, input_ids=aliases, source_refs=aliases)
    index["deferred"][0]["source_refs"] = aliases
    assert validate_index(index, input_ids=aliases, source_refs=aliases) is index


def test_first_extract_accepts_facts_only_without_fabricating_a_method_or_evidence_gap():
    aliases = ["unit-code-table", "unit-history"]
    index = {
        "schema_version": "skill-miner.index.v1", "input_ids": aliases,
        "methods": [], "deferred": [],
        "facts": [
            {"text": "产品代码与名称的对应关系。", "source_refs": aliases[:1]},
            {"text": "已提供的历史说明。", "source_refs": aliases[1:]},
        ],
    }
    original = copy.deepcopy(index)
    assert validate_index(index, input_ids=aliases, source_refs=aliases) == original
    assert index == original and not index["methods"] and not index["deferred"]
    with pytest.raises(ValueError, match="Every assigned source"):
        validate_index({**index, "facts": index["facts"][:1]}, input_ids=aliases, source_refs=aliases)
    with pytest.raises(ValueError, match="must contain supported"):
        validate_index({**index, "facts": []}, input_ids=aliases, source_refs=aliases)


def large_valid_index(alias):
    """Represent a real compact locator with a 252-character, source-specific boundary."""
    return {
        "schema_version": "skill-miner.index.v1",
        "input_ids": [alias],
        "methods": [
            {
                "task": "核对给定地区、单位、阈值和例外。" * 6,
                "boundary": ("只适用原始条款明确规定的条件，不能推断未提供的业务规则。" * 12)[:252],
                "source_refs": [alias],
            }
            for _ in range(4)
        ],
        "facts": [{"text": "逐项保留原文条件及其适用范围。" * 50, "source_refs": [alias]} for _ in range(5)],
        "deferred": [{"reason": "未说明的事实仍然需要补充原始 Evidence。" * 25, "source_refs": [alias]}],
    }


def test_source_specific_index_boundaries_fit_without_truncation_and_two_large_indexes_share_one_packet():
    aliases = ["unit-first", "unit-second"]
    indexes = [large_valid_index(alias) for alias in aliases]
    original = copy.deepcopy(indexes)
    for alias, index in zip(aliases, indexes):
        assert len(index["methods"][0]["boundary"]) == 252
        assert 6000 < len(json.dumps(index, ensure_ascii=False, separators=(",", ":"))) < 12000
        assert validate_index(index, input_ids=[alias], source_refs=[alias]) is index
    controls = {
        "inputs": [{"id": alias, "value": index} for alias, index in zip(aliases, indexes)],
        "input_ids": aliases,
        "allowed_source_refs": aliases,
        "final_merge": True,
    }
    packet = build_stage_packet("plan", "plan.json", controls=controls)
    assert len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) < 32000
    assert packet["derived_controls"]["inputs"] == controls["inputs"]
    assert indexes == original

    oversized_controls = {**controls, "inputs": controls["inputs"] * 6}
    with pytest.raises(ValueError, match="bounded input"):
        build_stage_packet("plan", "plan.json", controls=oversized_controls)
    indexes[0]["methods"][0]["source_refs"] = ["unit-unassigned"]
    with pytest.raises(ValueError, match="exact nonempty assigned references"):
        validate_index(indexes[0], input_ids=aliases[:1], source_refs=aliases[:1])


@pytest.mark.parametrize("group,field", [("methods", "task"), ("methods", "boundary"),
                                          ("facts", "text"), ("deferred", "reason")])
def test_index_field_flexibility_keeps_a_finite_limit(group, field):
    index = large_valid_index("unit-source")
    index[group][0][field] = "原" * 1001
    with pytest.raises(ValueError, match="at most 1000 characters"):
        validate_index(index, input_ids=["unit-source"], source_refs=["unit-source"])


def planning_catalog(method_count=38):
    units = [{
        "id": f"unit-{index}",
        "value": {
            "methods": [{
                "task": f"执行操作流程 {index}", "boundary": "按本条款核验条件", "source_refs": [f"scope-{index}"]
            }],
            "facts": [], "deferred": [],
        },
    } for index in range(method_count)]
    return units, planning_items(units)


def test_intermediate_merge_preserves_every_method_instead_of_selecting_zero_skills():
    units, items = planning_catalog()
    assert items == planning_items(units)
    value = {
        "schema_version": "skill-miner.merge-index.v1",
        "input_ids": [unit["id"] for unit in units],
        "methods": [{"task": item["task"], "boundary": item["boundary"], "derived_from": [item["item_id"]]}
                    for item in items],
        "facts": [], "deferred": [], "resolved": [],
    }
    original = copy.deepcopy(value)
    accepted = validate_merge(value, items=items, input_ids=value["input_ids"])
    assert len(accepted["methods"]) == 38
    assert [row["source_refs"] for row in accepted["methods"]] == [item["source_refs"] for item in items]
    assert value == original
    swallowed = {**value, "methods": [], "deferred": [
        {"reason": "历史版本信息不足", "derived_from": [item["item_id"] for item in items]}
    ]}
    with pytest.raises(ValueError, match="preserve method"):
        validate_merge(swallowed, items=items, input_ids=value["input_ids"])
    for changed in ({**value, "methods": value["methods"][:-1]},
                    {**value, "methods": value["methods"] + copy.deepcopy(value["methods"][:1])}):
        with pytest.raises(ValueError, match="every exact assigned"):
            validate_merge(changed, items=items, input_ids=value["input_ids"])


def test_final_selection_classifies_pure_facts_as_knowledge_without_inventing_a_gap():
    units = [{"id": "lookup", "value": {
        "methods": [], "facts": [{"text": "历史产品代码说明", "source_refs": ["scope-lookup"]}], "deferred": [],
    }}]
    items = planning_items(units)
    value = {
        "schema_version": "skill-miner.selection.v1", "input_ids": ["lookup"], "skills": [],
        "knowledge": [{"reason": "这是代码含义查表，没有独立操作流程", "derived_from": [items[0]["item_id"]]}],
        "deferred": [], "resolved": [],
    }
    accepted = validate_selection(value, items=items, input_ids=["lookup"])
    assert accepted["knowledge"][0]["source_refs"] == ["scope-lookup"]
    assert selection_plan(accepted) == {"schema_version": "skill-miner.plan.v1", "skills": [], "deferred": []}

    false_gap = copy.deepcopy(value)
    false_gap["deferred"] = false_gap.pop("knowledge")
    false_gap["knowledge"] = []
    original = copy.deepcopy(false_gap)
    with pytest.raises(ValueError, match="facts alone must remain knowledge"):
        validate_selection(false_gap, items=items, input_ids=["lookup"])
    assert false_gap == original


def test_selection_requires_each_unselected_method_reason_and_more_methods_need_more_windows():
    units, items = planning_catalog(2)
    value = {
        "schema_version": "skill-miner.selection.v1", "input_ids": [unit["id"] for unit in units],
        "skills": [], "knowledge": [], "resolved": [],
        "deferred": [{"reason": "缺少外部附件", "derived_from": [item["item_id"] for item in items]}],
    }
    with pytest.raises(ValueError, match="own explicit disposition"):
        validate_selection(value, items=items, input_ids=value["input_ids"])
    units, items = planning_catalog(9)
    value["input_ids"] = [unit["id"] for unit in units]
    with pytest.raises(ValueError, match="another window"):
        validate_selection(value, items=items, input_ids=value["input_ids"])


def test_fragment_gap_resolution_rejects_unrelated_file_support_and_retains_same_file_lineage():
    units = [{"id": "fragments", "value": {
        "methods": [],
        "facts": [{"text": "后续片段有完整计算规则", "source_refs": ["scope-later"]}],
        "deferred": [{"reason": "当前片段未包含后续计算规则", "source_refs": ["scope-first"]}],
    }}]
    files = {"scope-first": [URI], "scope-later": [URI + "-unrelated"]}
    items = planning_items(units, source_files=files)
    fact, gap = items
    value = {
        "schema_version": "skill-miner.merge-index.v1", "input_ids": ["fragments"], "methods": [],
        "facts": [{"text": fact["text"], "derived_from": [fact["item_id"]]}], "deferred": [],
        "resolved": [{"derived_from": [gap["item_id"]], "supported_by": [fact["item_id"]],
                      "reason": "原文件后续片段已提供同一流程的计算规则"}],
    }
    with pytest.raises(ValueError, match="same original source file"):
        validate_merge(value, items=items, input_ids=value["input_ids"])
    files["scope-later"] = [URI]
    items = planning_items(units, source_files=files)
    accepted = validate_merge(value, items=items, input_ids=value["input_ids"])
    assert accepted["facts"][0]["source_refs"] == ["scope-first", "scope-later"]
    assert accepted["resolved"][0]["source_refs"] == ["scope-first", "scope-later"]


def test_multi_file_gap_requires_explicit_support_for_every_affected_source_file():
    units = [{"id": "fragments", "value": {
        "methods": [],
        "facts": [
            {"text": "A后续片段有计算规则", "source_refs": ["scope-a-later"]},
            {"text": "B后续片段有计算规则", "source_refs": ["scope-b-later"]},
        ],
        "deferred": [{"reason": "A与B片段均未包含计算规则", "source_refs": ["scope-a", "scope-b"]}],
    }}]
    files = {"scope-a": [URI], "scope-a-later": [URI],
             "scope-b": [URI + "-b"], "scope-b-later": [URI + "-b"]}
    items = planning_items(units, source_files=files)
    fact_a, fact_b, gap = items
    value = {
        "schema_version": "skill-miner.merge-index.v1", "input_ids": ["fragments"], "methods": [],
        "facts": [{"text": item["text"], "derived_from": [item["item_id"]]} for item in (fact_a, fact_b)],
        "deferred": [],
        "resolved": [{"derived_from": [gap["item_id"]], "supported_by": [fact_a["item_id"]],
                      "reason": "仅A后续片段提供支持，不能据此声明B的缺口也已解决"}],
    }
    original = copy.deepcopy(value)
    with pytest.raises(ValueError, match="every affected file"):
        validate_merge(value, items=items, input_ids=value["input_ids"])
    assert value == original
    value["resolved"][0]["supported_by"].append(fact_b["item_id"])
    accepted = validate_merge(value, items=items, input_ids=value["input_ids"])
    assert accepted["resolved"][0]["source_refs"] == ["scope-a", "scope-a-later", "scope-b", "scope-b-later"]


def test_compact_plan_refs_are_current_aliases_and_all_sources_have_a_disposition():
    aliases = ["unit-first", "unit-second"]
    plan = {
        "schema_version": "skill-miner.plan.v1",
        "input_ids": aliases,
        "skills": [candidate(aliases[:1])],
        "deferred": [],
    }
    with pytest.raises(ValueError, match="Every assigned source"):
        validate_plan(plan, input_ids=aliases, source_refs=aliases)
    plan["deferred"] = [{"reason": "第二源没有独立方法", "source_refs": aliases[1:]}]
    assert validate_plan(plan, input_ids=aliases, source_refs=aliases) is plan
    plan["skills"][0]["source_refs"] = [URI]
    with pytest.raises(ValueError, match="exact nonempty"):
        validate_plan(plan, input_ids=aliases, source_refs=aliases)


def test_program_expanded_final_provenance_is_not_restricted_to_model_output_size():
    refs = [f"viking://resources/team/source/document-{index:04d}.md" for index in range(1000)]
    plan = {"schema_version": "skill-miner.plan.v1", "skills": [candidate(refs)], "deferred": []}
    with pytest.raises(ValueError, match="12000"):
        validate_plan(plan, source_refs=refs)
    assert validate_plan(plan, source_refs=refs, bounded=False) is plan


def test_section_composition_requires_all_raw_chunk_coverage_and_preserves_claims():
    chunks = source_chunks("9000 ≤ 12000；未拆封。" * 1200, URI, max_chars=800)
    sections = [
        {
            "schema_version": "skill-miner.section.v1",
            "input_ids": [chunk["id"]],
            "title": f"原文分段 {index}",
            "content": chunk["content"],
        }
        for index, chunk in enumerate(chunks, 1)
    ]
    expected = [chunk["id"] for chunk in chunks]
    result = compose_skill(candidate([URI]), sections, expected_input_ids=expected)

    validate_skill_md(result, expected_name="return-check")
    assert parse_skill_md_text(result)["description"] == "核验退货申请"
    assert all(section["content"] in result for section in sections)
    with pytest.raises(ValueError, match="every exact assigned"):
        compose_skill(candidate([URI]), sections[:-1], expected_input_ids=expected)
    with pytest.raises(ValueError, match="every exact assigned"):
        compose_skill(candidate([URI]), sections + sections[:1], expected_input_ids=expected)
    with pytest.raises(ValueError):
        compose_skill(candidate([URI]), [], expected_input_ids=[])


def test_skipped_source_ranges_are_audited_without_withdrawing_a_supported_method():
    skipped = {
        "schema_version": "skill-miner.section-skip.v1",
        "input_ids": ["intro"],
        "reason": "本片段仅含历史版本，不包含当前方法的额外操作条件。",
    }
    operational = {
        "schema_version": "skill-miner.section.v1", "input_ids": ["method"],
        "title": "受理核验", "content": "按原文条件核验申请并记录判断。",
    }
    assert validate_section_skip(skipped, input_ids=["intro"]) is skipped
    rendered = compose_skill(candidate([URI]), [skipped, operational], expected_input_ids=["intro", "method"])
    assert operational["content"] in rendered
    assert skipped["reason"] not in rendered
    assert "## 1. 受理核验" in rendered
    with pytest.raises(ValueError, match="every exact assigned"):
        compose_skill(candidate([URI]), [operational], expected_input_ids=["intro", "method"])
    with pytest.raises(ValueError, match="at least one"):
        compose_skill(candidate([URI]), [skipped], expected_input_ids=["intro"])
    with pytest.raises(ValueError, match="every exact assigned"):
        validate_section_skip(skipped, input_ids=["another"])
    with pytest.raises(ValueError, match="at most 2000"):
        validate_section_skip({**skipped, "reason": "原" * 2001}, input_ids=["intro"])


def test_streamed_skill_matches_previous_canonical_bytes_with_skips_and_trailing_whitespace():
    proposed = candidate([URI])
    proposed["boundary"] += "  \n"
    sections = [
        {"schema_version": "skill-miner.section-skip.v1", "input_ids": ["intro"], "reason": "历史背景"},
        {"schema_version": "skill-miner.section.v1", "input_ids": ["first"],
         "title": "资格核验", "content": "核验已提供的凭证。  \n\t"},
        {"schema_version": "skill-miner.section-skip.v1", "input_ids": ["middle"], "reason": "无额外方法"},
        {"schema_version": "skill-miner.section.v1", "input_ids": ["last"],
         "title": "例外处理", "content": "无法确认时暂停并记录缺口。 \n\n\t"},
        {"schema_version": "skill-miner.section-skip.v1", "input_ids": ["tail"], "reason": "附件索引"},
    ]
    original = copy.deepcopy(sections)
    body = f"# {proposed['task']}\n\n{proposed['boundary']}\n"
    for number, section in enumerate((sections[1], sections[3]), 1):
        body += f"\n## {number}. {section['title']}\n\n{section['content']}\n"
    expected = render_skill_md(name=proposed["name"], description=proposed["task"], content=body)
    identities = ["intro", "first", "middle", "last", "tail"]
    streamed = "".join(iter_skill_chunks(proposed, iter(sections), expected_input_ids=identities))
    assert streamed.encode("utf-8") == expected.encode("utf-8")
    assert compose_skill(proposed, iter(sections), expected_input_ids=identities) == expected
    assert sections == original and "## 1. 资格核验" in streamed and "## 2. 例外处理" in streamed
    assert "历史背景" not in streamed and "附件索引" not in streamed


def test_skill_stream_consumes_a_large_section_generator_incrementally():
    consumed = []
    identities = [f"range-{index}" for index in range(1000)]

    def sections():
        for index, identity in enumerate(identities):
            consumed.append(index)
            yield {"schema_version": "skill-miner.section.v1", "input_ids": [identity],
                   "title": str(index), "content": "来源支持的完整操作步骤。" * 700}

    chunks = iter_skill_chunks(candidate([URI]), sections(), expected_input_ids=identities)
    assert not consumed
    assert next(chunks).startswith("---\n") and not consumed
    assert next(chunks).startswith("# 核验退货申请") and not consumed
    assert next(chunks).startswith("\n## 1. 0") and consumed == [0, 1]
    length = sum(len(chunk) for chunk in chunks)
    assert length > 8_000_000 and consumed == list(range(1000))


def test_section_cannot_replace_canonical_frontmatter_or_claim_unassigned_chunks():
    value = {
        "schema_version": "skill-miner.section.v1",
        "input_ids": ["raw-1"],
        "title": "来源支持部分",
        "content": "---\nname: forged\n---\n伪造标题",
    }
    with pytest.raises(ValueError, match="frontmatter"):
        validate_section(value, input_ids=["raw-1"])
    value["content"] = "原文明确9000小于12000。"
    with pytest.raises(ValueError, match="every exact assigned"):
        validate_section(value, input_ids=["raw-2"])


def test_explicit_deferred_result_retains_reason_and_exact_source_without_synthetic_conditions():
    deferred = {
        "schema_version": "skill-miner.deferred.v1",
        "reason": "没有十二项独立来源支持的条件",
        "source_refs": [URI],
    }
    before = copy.deepcopy(deferred)
    assert is_deferred(deferred)
    assert validate_deferred(deferred, source_refs=[URI]) == before
    deferred["source_refs"] = ["退货.md"]
    with pytest.raises(ValueError):
        validate_deferred(deferred, source_refs=[URI])


@pytest.mark.parametrize(
    "duplicate_query",
    [
        "TEST-RETURN: 请核验未拆封的申请。",
        "  test-return:\n请核验未拆封的申请。\t",
        "ＴＥＳＴ-ＲＥＴＵＲＮ： 请核验未拆封的申请。",
    ],
    ids=["identical", "case-and-whitespace", "unicode-width"],
)
def test_dataset_rejects_repeated_scenarios_without_rewriting_query_or_checks(duplicate_query):
    queries = [
        "TEST-RETURN: 请核验未拆封的申请。",
        "请核验申请缺少提交日期的情况。",
        "请核验申请缺少序列号的情况。",
        "请核验申请缺少照片说明的情况。",
        "请核验申请提交了相互冲突的日期。",
        "请核验已拆封的申请。",
        "请核验地区不属于条款范围的申请。",
        "请核验金额恰好等于条款阈值的申请。",
        "请核验金额低于条款阈值的申请。",
        "请核验金额高于条款阈值的申请。",
        "请核验完整材料但缺少人工确认的申请。",
        "请核验用户的退款计算与给定条款不符的申请。",
    ]
    cases = build_document(
        "return-check",
        [
            {
                "case_id": f"return-check-case-{index:02d}",
                "query": query,
                "requirements": [f"Preserve synthetic source condition {number}." for number in range(1, 13)],
                "source_session_ids": [URI],
            }
            for index, query in enumerate(queries, 1)
        ],
    )["cases"]
    original = copy.deepcopy(cases)
    document = assemble_dataset(cases, skill_name="return-check", task="核验合成申请", identity="unique")
    assert document["cases"] == cases == original

    cases[1]["query"] = duplicate_query
    before_rejection = copy.deepcopy(cases)
    with pytest.raises(ValueError, match="distinct normalized queries.*case-02 repeats.*case-01"):
        assemble_dataset(cases, skill_name="return-check", task="核验合成申请", identity="duplicate")
    assert cases == before_rejection


def _custom_identity_cases():
    return build_document(
        "return-check",
        [
            {"case_id": f"reviewed-return-{index:03d}", "query": f"独立合成申请场景 {index}",
             "source_session_ids": [URI]}
            for index in range(12)
        ],
    )["cases"]


def test_dataset_can_preserve_reviewed_case_identities_without_rewriting_cases():
    cases = _custom_identity_cases()
    expected = [case["case_id"] for case in cases]
    original = copy.deepcopy(cases)
    document = assemble_dataset(
        cases, skill_name="return-check", task="核验合成申请", identity="reviewed",
        expected_case_ids=expected,
    )
    assert document["cases"] == cases == original
    assert [case["case_id"] for case in document["cases"]] == expected
    with pytest.raises(ValueError, match="stable order"):
        assemble_dataset(cases, skill_name="return-check", task="核验合成申请", identity="default")
    with pytest.raises(ValueError, match="stable order"):
        assemble_dataset(
            cases, skill_name="return-check", task="核验合成申请", identity="wrong-order",
            expected_case_ids=list(reversed(expected)),
        )
    assert cases == original


@pytest.mark.parametrize(
    "invalid_ids",
    [
        [], [f"id-{index}" for index in range(11)], [f"id-{index}" for index in range(13)],
        ["duplicate"] * 12,
        ["", *[f"id-{index}" for index in range(11)]],
        [None, *[f"id-{index}" for index in range(11)]],
        ["contains space", *[f"id-{index}" for index in range(11)]],
        ["../outside", *[f"id-{index}" for index in range(11)]],
        ["folder\\outside", *[f"id-{index}" for index in range(11)]],
        ["control\x00id", *[f"id-{index}" for index in range(11)]],
        ["x" * 161, *[f"id-{index}" for index in range(11)]],
    ],
)
def test_dataset_rejects_invalid_preserved_case_identities(invalid_ids):
    cases = _custom_identity_cases()
    original = copy.deepcopy(cases)
    with pytest.raises(ValueError, match="twelve unique safe nonempty"):
        assemble_dataset(
            cases, skill_name="return-check", task="核验合成申请", identity="invalid",
            expected_case_ids=invalid_ids,
        )
    assert cases == original
