"""Lossless method partition contracts; no model, filesystem or network work."""

import copy
import hashlib
import json

import pytest

from team_miner.workflow_partition import (
    CAPACITY_SCHEMA,
    METHOD_COLUMNS,
    OUTPUT_SCHEMA,
    SCOPE_COLUMNS,
    WorkflowPartitionManifestError,
    WorkflowPartitionOutputError,
    build_workflow_partition,
    restore_workflow_partition,
    validate_workflow_partition_manifest,
    validate_workflow_partition_output,
)


def record(index, *, task=None, boundary=None, source=None):
    source = source or f"viking://source/文档{index}.md"
    identity, scope = f"original-method-{index}", f"original-scope-{index}"
    return {"item": {"kind": "method", "item_id": identity, "task": task or f"完成方法{index}",
                     "boundary": boundary or "保留完整条件，不自动修正原文冲突。", "source_refs": [scope],
                     "source_files": [source]}, "unit_id": f"original-unit-{index}",
            "ordinals": [index], "bindings": {scope: [index]}, "leaf_items": [identity],
            "original_extra": {"untouched": ["原始档案", index]}}


def assignment(records=None, **limits):
    built = build_workflow_partition(records if records is not None else [record(i) for i in range(4)], **limits)
    assert built["status"] == "ready"
    manifest = built["manifest"]
    value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": manifest["assignment_sha256"],
             "rows": [[identity, identity] for identity in manifest["records"]], "reasons": {}}
    return built, value


def sign_manifest(manifest):
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def test_complete_late_parent_across_documents_restores_all_records_and_union_provenance():
    records = [record(0, task="电话中确认预约时间"), record(1, task="征得对话同意"),
               record(2, task="完整约访：自我介绍、征得同意、提出时间、处理异议并确认约会。")]
    before = copy.deepcopy(records)
    built, value = assignment(records)
    controls, manifest = built["controls"], built["manifest"]
    assert controls["method_columns"] == METHOD_COLUMNS == ["id", "task", "boundary", "scope_id"]
    assert [row[0] for row in controls["method_rows"]] == ["m0001", "m0002", "m0003"]
    assert controls["method_rows"][-1][1] == records[-1]["item"]["task"]
    assert [row[3] for row in controls["method_rows"]] == ["p1", "p2", "p3"]
    assert controls["source_relationships"]["scope_columns"] == SCOPE_COLUMNS == ["id", "files", "refs"]
    assert controls["source_relationships"]["scope_rows"] == [
        [f"p{i + 1}", [f"s{i + 1}"], [f"r{i + 1}"]] for i in range(3)
    ]
    assert manifest["sources"]["files"]["s3"] == records[-1]["item"]["source_files"][0]
    value.update(rows=[[identity, "m0003"] for identity in manifest["records"]],
                 reasons={"m0003": "现有完整方法包含前两项步骤，跨文档来源均保留。"})
    snapshot = copy.deepcopy((value, manifest))
    restored = restore_workflow_partition(value, manifest)
    group = restored["groups"][0]
    assert group["parent_record"] == records[2]
    assert group["records"] == records
    assert group["provenance"]["ordinals"] == [0, 1, 2]
    assert set(group["provenance"]["source_files"]) == {row["item"]["source_files"][0] for row in records}
    assert group["provenance"]["bindings"] == {f"original-scope-{i}": [i] for i in range(3)}
    assert group["provenance"]["leaf_items"] == ["original-method-0", "original-method-1", "original-method-2"]
    assert records == before and (value, manifest) == snapshot
    group["parent_record"]["item"]["task"] = "不应写回"
    group["records"][0]["original_extra"]["untouched"].clear()
    assert records == before and (value, manifest) == snapshot


def test_shared_source_support_is_relationship_only_and_can_stay_independent():
    records = [record(i, source="viking://source/共享.md") for i in range(3)]
    # Two source references can bind the same original source range.
    records[1]["ordinals"] = [0]
    records[1]["bindings"] = {"original-scope-1": [0]}
    built, value = assignment(records)
    scopes = built["controls"]["source_relationships"]["scope_rows"]
    assert all(scope[1] == ["s1"] for scope in scopes)
    assert {ref for scope in scopes for ref in scope[2]} == {"r1", "r2", "r3"}
    restored = restore_workflow_partition(value, built["manifest"])
    assert len(restored["independent"]) == 3 and restored["groups"] == []
    assert [row["parent_record"] for row in restored["independent"]] == records


def test_identical_scopes_are_shared_without_losing_source_sets_or_original_order():
    records = [record(i, source="viking://source/共享.md") for i in range(3)]
    for index, row in enumerate(records):
        row["item"]["source_files"] += ["viking://source/另一来源.md"]
        row["item"]["source_refs"] = ["first-scope", "second-scope"]
        row["bindings"] = {"first-scope": [0], "second-scope": [1]}
        row["ordinals"] = [0, 1]
        if index == 1:
            row["item"]["source_files"].reverse()
            row["item"]["source_refs"].reverse()
    before = copy.deepcopy(records)
    built, value = assignment(records)
    assert [row[3] for row in built["controls"]["method_rows"]] == ["p1"] * 3
    assert built["controls"]["source_relationships"] == {
        "scope_columns": ["id", "files", "refs"], "scope_rows": [["p1", ["s1", "s2"], ["r1", "r2"]]]
    }
    restored = restore_workflow_partition(value, built["manifest"])
    assert [row["parent_record"] for row in restored["independent"]] == records == before
    built["controls"]["source_relationships"]["scope_rows"][0][1].clear()
    assert built["manifest"]["sources"]["scopes"]["p1"]["files"] == ["s1", "s2"]
    with pytest.raises(WorkflowPartitionManifestError):
        validate_workflow_partition_manifest(built["manifest"], controls=built["controls"])


def test_full_business_boundary_is_never_trimmed_and_manifest_binds_the_input():
    boundary = " 前提：客户自愿同意；" + "退休不会立刻下降，之后缓慢下降。" * 80 + " 保留末尾限定语。 "
    task = "完整原文含 m0001 和 s0001 字面文本。" * 35
    built, value = assignment([record(0, task=task, boundary=boundary)])
    assert built["controls"]["method_rows"][0][2] == boundary
    assert built["controls"]["method_rows"][0][1] == task
    validate_workflow_partition_manifest(built["manifest"], controls=built["controls"])
    restored = restore_workflow_partition(value, built["manifest"])
    assert restored["independent"][0]["parent_record"]["item"]["boundary"] == boundary


def test_every_method_can_be_independent_without_a_target_group_count():
    built, value = assignment([record(i, source="viking://source/完整.md") for i in range(154)])
    accepted = validate_workflow_partition_output(value, built["manifest"])
    assert accepted == value
    assert built["capacity"]["all_independent_chars"] <= built["capacity"]["worst_alias_structure_chars"] <= 12000
    restored = restore_workflow_partition(value, built["manifest"])
    assert len(restored["independent"]) == 154
    assert len({row["parent_record"]["item"]["item_id"] for row in restored["independent"]}) == 154


def test_knowledge_and_deferred_are_explicit_primary_dispositions_with_full_records():
    built, value = assignment()
    value.update(rows=[["m0001", "m0002"], ["m0002", "m0002"], ["m0003", "knowledge"], ["m0004", "deferred"]],
                 reasons={"m0002": "完整父方法", "m0003": "仅静态画像，没有可执行方法。",
                          "m0004": "原文缺少必要附件，不能推断。"})
    restored = restore_workflow_partition(value, built["manifest"])
    assert restored["knowledge"][0]["parent_record"] == record(2)
    assert restored["deferred"][0]["records"] == [record(3)]
    assert restored["deferred"][0]["reason"] == value["reasons"]["m0004"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "object_alias", "unknown_parent",
                                  "chain", "cycle", "reorder", "parent_to_knowledge", "extra_reason",
                                  "missing_reason", "empty_reason", "long_reason",
                                  "extra_field", "wrong_schema", "wrong_assignment"])
def test_rejects_ambiguous_partial_unknown_or_unauthorized_dispositions_without_mutation(fault):
    built, value = assignment()
    if fault == "missing":
        value["rows"].pop()
    elif fault == "duplicate":
        value["rows"][-1] = ["m0001", "m0001"]
    elif fault == "unknown":
        value["rows"][-1][0] = "m9999"
    elif fault == "object_alias":
        value["rows"][0] = {"method_id": "m0001", "destination": "m0001"}
    elif fault == "unknown_parent":
        value["rows"][0][1] = "m9999"
    elif fault == "reorder":
        value["rows"].reverse()
    elif fault in {"chain", "cycle", "parent_to_knowledge"}:
        value["rows"][0][1] = "m0002"
        value["rows"][1][1] = {"chain": "m0003", "cycle": "m0001", "parent_to_knowledge": "knowledge"}[fault]
    elif fault in {"empty_reason", "long_reason", "missing_reason", "extra_reason"}:
        value["rows"][0][1] = "m0002"
        value["reasons"] = {"m0002": "完整方法包含关系"}
        if fault == "empty_reason":
            value["reasons"]["m0002"] = "  "
        elif fault == "long_reason":
            value["reasons"]["m0002"] = "据" * 201
        elif fault == "missing_reason":
            value["reasons"].clear()
        else:
            value["reasons"]["m0003"] = "不允许多余的独立原因"
    elif fault == "extra_field":
        value["title"] = "不能扩展协议"
    elif fault == "wrong_schema":
        value["schema_version"] = "skill-miner.workflow-partition.v1"
    else:
        value["assignment_sha256"] = "0" * 64
    before = copy.deepcopy(value)
    with pytest.raises(WorkflowPartitionOutputError):
        validate_workflow_partition_output(value, built["manifest"])
    assert value == before


@pytest.mark.parametrize("fault", ["record", "record_hash", "source_map", "scope_map", "limits", "assignment",
                                  "controls_hash"])
def test_resigned_manifest_cannot_hide_record_source_or_assignment_tampering(fault):
    built, value = assignment()
    manifest = built["manifest"]
    if fault == "record":
        manifest["records"]["m0001"]["record"]["item"]["boundary"] += " 被篡改"
    elif fault == "record_hash":
        manifest["records"]["m0001"]["record_sha256"] = "0" * 64
    elif fault == "source_map":
        manifest["sources"]["files"]["s1"] = "viking://source/另一文档.md"
    elif fault == "scope_map":
        manifest["sources"]["scopes"]["p1"]["refs"] = ["r2"]
    elif fault == "limits":
        manifest["limits"]["max_output_chars"] = 12001
    elif fault == "assignment":
        manifest["assignment_sha256"] = "0" * 64
    else:
        manifest["controls_sha256"] = "0" * 64
    sign_manifest(manifest)
    with pytest.raises(WorkflowPartitionManifestError):
        validate_workflow_partition_output(value, manifest)


def test_controls_and_output_cannot_be_rebound_to_a_different_batch_with_the_same_aliases():
    first, value = assignment([record(0)])
    second, _ = assignment([record(1)])
    assert first["controls"]["assigned_ids"] == second["controls"]["assigned_ids"]
    with pytest.raises(WorkflowPartitionManifestError):
        validate_workflow_partition_manifest(first["manifest"], controls=second["controls"])
    with pytest.raises(WorkflowPartitionOutputError, match="assignment_sha256"):
        validate_workflow_partition_output(value, second["manifest"])


@pytest.mark.parametrize("kind", ["methods", "controls", "output"])
def test_capacity_returns_explicit_batching_without_partial_controls_or_truncation(kind):
    records = [record(i) for i in range(4)]
    limits = {"methods": {"max_methods": 3}, "controls": {"max_controls_chars": 100},
              "output": {"max_output_chars": 100}}[kind]
    before = copy.deepcopy(records)
    built = build_workflow_partition(records, **limits)
    assert built["status"] == "needs_batching" and built["reason"]
    assert built["controls"] is None and built["manifest"] is None
    assert built["capacity"]["method_count"] == 4 and records == before


def test_serialized_capacity_accounts_for_json_escaping_not_just_text_lengths():
    records = [record(0, task='"\\\n' * 200)]
    built, _ = assignment(records)
    exact = len(json.dumps(built["controls"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert built["capacity"]["controls_chars"] == exact
    assert build_workflow_partition(records, max_controls_chars=exact)["status"] == "ready"
    assert build_workflow_partition(records, max_controls_chars=exact - 1)["status"] == "needs_batching"


def test_actual_reason_text_is_bounded_by_total_output_even_when_alias_structure_fits():
    built, value = assignment([record(i, source="viking://source/完整.md") for i in range(60)])
    value.update(rows=[[identity, "knowledge"] for identity in built["controls"]["assigned_ids"]],
                 reasons={identity: "据" * 200 for identity in built["controls"]["assigned_ids"]})
    with pytest.raises(WorkflowPartitionOutputError, match="output limit"):
        validate_workflow_partition_output(value, built["manifest"])


def test_capacity_signal_requires_complete_identity_and_never_restores_partial_work():
    built, _ = assignment()
    capacity = {"schema_version": CAPACITY_SCHEMA, "assignment_sha256": built["manifest"]["assignment_sha256"],
                "input_ids": built["controls"]["assigned_ids"], "reason": "完整分组输出无法容纳，请宿主分批。"}
    before = copy.deepcopy(capacity)
    assert validate_workflow_partition_output(capacity, built["manifest"]) == capacity
    with pytest.raises(WorkflowPartitionOutputError, match="host batching"):
        restore_workflow_partition(capacity, built["manifest"])
    assert capacity == before
    for bad in ({**capacity, "input_ids": capacity["input_ids"][:-1]},
                {**capacity, "input_ids": [*capacity["input_ids"], "m0001"]},
                {**capacity, "input_ids": list(reversed(capacity["input_ids"]))},
                {**capacity, "groups": []}):
        with pytest.raises(WorkflowPartitionOutputError):
            validate_workflow_partition_output(bad, built["manifest"])


@pytest.mark.parametrize("fault", ["duplicate_id", "missing_boundary", "bad_ordinals", "lost_binding", "nan"])
def test_invalid_catalog_records_are_not_silently_repaired(fault):
    records = [record(i) for i in range(2)]
    if fault == "duplicate_id":
        records[1]["item"]["item_id"] = records[0]["item"]["item_id"]
    elif fault == "missing_boundary":
        records[0]["item"].pop("boundary")
    elif fault == "bad_ordinals":
        records[0]["ordinals"] = [False]
    elif fault == "lost_binding":
        records[0]["bindings"] = {}
    else:
        records[0]["extra"] = float("nan")
    with pytest.raises(WorkflowPartitionManifestError):
        build_workflow_partition(records)


@pytest.mark.parametrize("limits", [{"max_methods": 513}, {"max_controls_chars": 40001},
                                     {"max_output_chars": 12001}, {"max_methods": True}])
def test_single_batch_limits_cannot_be_disabled(limits):
    with pytest.raises(ValueError):
        build_workflow_partition([record(0)], **limits)
