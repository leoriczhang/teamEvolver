"""Single-workflow metadata uses frozen groups and host-derived exact provenance."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner.skill_miner.workflow_metadata import (
    OUTPUT_SCHEMA,
    WorkflowMetadataCapacityError,
    WorkflowMetadataManifestError,
    WorkflowMetadataOutputError,
    build_workflow_metadata,
    restore_workflow_metadata,
    validate_workflow_metadata_manifest,
    validate_workflow_metadata_output,
)
from team_miner.skill_miner.workflow_partition import (
    build_workflow_partition,
    restore_workflow_partition,
)


def _record(index, *, ordinals=None, task=None):
    indexes = [index] if ordinals is None else ordinals
    refs = [f"scope-{ordinal}" for ordinal in indexes]
    return {
        "item": {"item_id": f"original-method-{index}", "kind": "method",
                 "task": task or f"完整处理请求流程及条件分支{index}", "boundary": f"遵守完整边界和异常条件{index}",
                 "source_refs": refs, "source_files": [f"viking://original/source-{index}.md"]},
        "ordinals": indexes, "bindings": {ref: [ordinal] for ref, ordinal in zip(refs, indexes)},
        "unit_id": f"unit-{index}", "leaf_items": [f"leaf-{index}-a", f"leaf-{index}-b"],
    }


def _group(*, independent=False):
    records = [_record(0, ordinals=[0, 2]), _record(1, ordinals=[1, 2])]
    built = build_workflow_partition(records)
    controls, manifest = built["controls"], built["manifest"]
    ids = controls["assigned_ids"]
    output = {
        "schema_version": controls["output_schema"], "assignment_sha256": controls["assignment_sha256"],
        "rows": [[identity, identity if independent else ids[-1]] for identity in ids],
        "reasons": {} if independent else {ids[-1]: "完整流程覆盖条件分支"},
    }
    restored = restore_workflow_partition(output, manifest)
    return restored["independent" if independent else "groups"][0]


def _candidate(manifest):
    return {
        "schema_version": OUTPUT_SCHEMA, "assignment_sha256": manifest["assignment_sha256"],
        "candidate": {"name": "request-resolution", "task": "完整处理请求",
                      "trigger": "收到待处理请求", "inputs": ["请求内容和适用条件"], "outputs": ["可核对的处理结论"],
                      "boundary": "保留原始约束和例外", "rationale": "原文支持完整独立方法"},
    }


@pytest.mark.parametrize("independent", [False, True])
def test_complete_partition_group_and_independent_use_the_same_metadata_contract(independent):
    group = _group(independent=independent)
    before = copy.deepcopy(group)
    controls, manifest = build_workflow_metadata(group)
    validate_workflow_metadata_manifest(manifest, controls=controls)
    assert group == before
    assert controls["parent_id"] == group["parent_id"]
    assert controls["member_ids"] == group["member_ids"]
    assert controls["method_columns"] == ["id", "task", "boundary", "scope_id"]
    assert [row[1] for row in controls["method_rows"]] == [row["item"]["task"] for row in group["records"]]
    assert [row[2] for row in controls["method_rows"]] == [row["item"]["boundary"] for row in group["records"]]
    assert set(manifest["member_sha256"]) == set(group["member_ids"])
    raw = _candidate(manifest)
    restored = restore_workflow_metadata(raw, manifest, language_policy={"language": "zh-CN"})
    assert restored["candidate"]["derived_from"] == [row["item"]["item_id"] for row in group["records"]]
    assert restored["candidate"]["source_refs"] == group["provenance"]["source_refs"]
    assert restored["provenance"] == group["provenance"]
    assert "source_refs" not in raw["candidate"] and "derived_from" not in raw["candidate"]
    restored["provenance"]["leaf_items"].clear()
    restored["candidate"]["source_refs"].clear()
    assert manifest["group"] == before
    assert build_workflow_metadata(group) == (controls, manifest)


def test_deferral_preserves_all_members_and_shared_source_bindings():
    group = _group()
    _, manifest = build_workflow_metadata(group)
    value = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": manifest["assignment_sha256"],
             "deferred": {"reason": "现有索引不能支持父方法的完整独立范围"}}
    restored = restore_workflow_metadata(value, manifest)
    assert restored["deferred"]["derived_from"] == ["original-method-0", "original-method-1"]
    assert restored["deferred"]["source_refs"] == ["scope-0", "scope-2", "scope-1"]
    assert restored["provenance"]["ordinals"] == [0, 1, 2]
    assert restored["provenance"]["bindings"] == {"scope-0": [0], "scope-1": [1], "scope-2": [2]}
    assert len(restored["provenance"]["source_files"]) == 2
    assert len(restored["provenance"]["leaf_items"]) == 4


@pytest.mark.parametrize("fault", ["missing-member", "duplicate-member", "duplicate-original", "foreign-parent",
                                  "parent-record", "provenance", "conflicting-binding", "unclassified-field"])
def test_builder_rejects_corrupt_group_or_incomplete_lineage(fault):
    group = _group()
    if fault == "missing-member":
        group["records"].pop()
    elif fault == "duplicate-member":
        group["member_ids"][1] = group["member_ids"][0]
    elif fault == "duplicate-original":
        group["records"][1]["item"]["item_id"] = group["records"][0]["item"]["item_id"]
    elif fault == "foreign-parent":
        group["parent_id"] = "outside-group"
    elif fault == "parent-record":
        group["parent_record"] = group["records"][0]
    elif fault == "provenance":
        group["provenance"]["source_refs"].pop()
    elif fault == "conflicting-binding":
        group["records"][1]["bindings"]["scope-2"] = [3]
        group["records"][1]["ordinals"] = [1, 3]
    else:
        group["known_candidates"] = []
    with pytest.raises(WorkflowMetadataManifestError):
        build_workflow_metadata(group)


@pytest.mark.parametrize("fault", ["text", "member-hash", "group-hash", "source", "assignment", "limit", "extra"])
def test_manifest_cannot_hide_frozen_text_source_mapping_or_digest_changes(fault):
    controls, manifest = build_workflow_metadata(_group())
    if fault == "text":
        manifest["group"]["records"][0]["item"]["task"] += "更改范围"
    elif fault == "member-hash":
        manifest["member_sha256"]["m0001"] = "0" * 64
    elif fault == "group-hash":
        manifest["group_sha256"] = "0" * 64
    elif fault == "source":
        manifest["sources"]["references"]["r1"] = "foreign-scope"
    elif fault == "assignment":
        manifest["assignment_sha256"] = "0" * 64
    elif fault == "limit":
        manifest["limits"]["max_output_chars"] = 11999
    else:
        manifest["repair"] = True
    with pytest.raises(WorkflowMetadataManifestError):
        validate_workflow_metadata_manifest(manifest, controls=controls)


def test_complete_text_is_never_clipped_to_make_controls_or_stage_packet_fit():
    group = _group()
    group["records"][0]["item"]["task"] = "完整约束" * 1800
    group["records"][0]["item"]["boundary"] = "完整条件" * 700
    before = copy.deepcopy(group)
    controls, manifest = build_workflow_metadata(group)
    assert controls["method_rows"][0][1] == group["records"][0]["item"]["task"]
    assert controls["method_rows"][0][2] == group["records"][0]["item"]["boundary"]
    assert manifest["group"] == before and group == before
    with pytest.raises(WorkflowMetadataCapacityError):
        build_workflow_metadata(group, max_controls_chars=1000)
    assert group == before
    from team_miner.skill_miner.bounded_compile_plan import build_stage_packet

    with pytest.raises(ValueError, match="bounded input budget"):
        build_stage_packet("workflow_metadata", "metadata.json", controls=controls, max_chars=1000)
    assert group == before


@pytest.mark.parametrize("fault", ["extra", "schema", "hash", "two-candidates", "both", "no-result",
                                  "source_refs", "derived_from", "member_ids", "missing-field", "bad-slug",
                                  "too-long", "no-inputs", "too-many-outputs", "long-input", "bad-deferred"])
def test_output_cannot_override_single_workflow_contract_or_host_provenance(fault):
    _, manifest = build_workflow_metadata(_group())
    raw = _candidate(manifest)
    if fault == "extra":
        raw["input_ids"] = ["m0001"]
    elif fault == "schema":
        raw["schema_version"] = "skill-miner.selection.v2"
    elif fault == "hash":
        raw["assignment_sha256"] = "0" * 64
    elif fault == "two-candidates":
        raw["candidate"] = [raw["candidate"], copy.deepcopy(raw["candidate"])]
    elif fault == "both":
        raw["deferred"] = {"reason": "不能同时暂缓和接受"}
    elif fault == "no-result":
        raw.pop("candidate")
    elif fault in {"source_refs", "derived_from", "member_ids"}:
        raw["candidate"][fault] = ["foreign-scope"]
    elif fault == "missing-field":
        raw["candidate"].pop("boundary")
    elif fault == "bad-slug":
        raw["candidate"]["name"] = "中文-title"
    elif fault == "too-long":
        raw["candidate"]["task"] = "字" * 401
    elif fault == "no-inputs":
        raw["candidate"]["inputs"] = []
    elif fault == "too-many-outputs":
        raw["candidate"]["outputs"] = ["结果"] * 17
    elif fault == "long-input":
        raw["candidate"]["inputs"] = ["字" * 241]
    else:
        raw.pop("candidate")
        raw["deferred"] = {"reason": "具体限制", "source_refs": ["scope-0"]}
    with pytest.raises(WorkflowMetadataOutputError):
        restore_workflow_metadata(raw, manifest)


@pytest.mark.parametrize("field", ["task", "trigger", "boundary", "rationale"])
def test_chinese_policy_rejects_untranslated_human_metadata_but_keeps_ascii_machine_name(field):
    _, manifest = build_workflow_metadata(_group())
    raw = _candidate(manifest)
    raw["candidate"][field] = "An untranslated generated description"
    with pytest.raises(WorkflowMetadataOutputError, match="中文"):
        validate_workflow_metadata_output(raw, manifest, language_policy={"language": "zh-CN"})
    assert validate_workflow_metadata_output(raw, manifest, language_policy={"language": "en"}) == raw
    assert validate_workflow_metadata_output(raw, manifest) == raw


def test_chinese_deferral_and_actual_output_capacity_are_validated():
    _, manifest = build_workflow_metadata(_group())
    raw = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": manifest["assignment_sha256"],
           "deferred": {"reason": "This generated explanation is untranslated"}}
    with pytest.raises(WorkflowMetadataOutputError, match="中文"):
        restore_workflow_metadata(raw, manifest, language_policy={"language": "zh-CN"})
    _, small_manifest = build_workflow_metadata(_group(), max_output_chars=200)
    raw = _candidate(small_manifest)
    assert len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"))) > 200
    with pytest.raises(WorkflowMetadataOutputError, match="bounded"):
        validate_workflow_metadata_output(raw, small_manifest)


@pytest.mark.parametrize("field", ["inputs", "outputs"])
def test_chinese_policy_checks_array_descriptions_without_translating_machine_values(field):
    _, manifest = build_workflow_metadata(_group())
    raw = _candidate(manifest)
    raw["candidate"][field] = ["request_id", "The complete approved request"]
    with pytest.raises(WorkflowMetadataOutputError, match="中文"):
        validate_workflow_metadata_output(raw, manifest, language_policy={"language": "zh-CN"})
    assert validate_workflow_metadata_output(raw, manifest, language_policy={"language": "en"}) == raw
    raw["candidate"][field] = [
        "request_id", "result.json", "/tmp/request.json", "viking://source/example.md", "12000",
        "`requestId`", "```json\n{\"status\": \"ready\"}\n```", "JSON 请求内容及 request_id",
    ]
    checked = validate_workflow_metadata_output(raw, manifest, language_policy={"language": "zh-CN"})
    assert checked == raw


def test_same_business_metadata_on_different_groups_cannot_reuse_assignment_hash():
    _, grouped_manifest = build_workflow_metadata(_group())
    _, independent_manifest = build_workflow_metadata(_group(independent=True))
    assert grouped_manifest["assignment_sha256"] != independent_manifest["assignment_sha256"]
    with pytest.raises(WorkflowMetadataOutputError, match="another frozen assignment"):
        validate_workflow_metadata_output(_candidate(grouped_manifest), independent_manifest)


def test_published_controls_must_match_the_frozen_manifest():
    controls, manifest = build_workflow_metadata(_group())
    controls["method_rows"][0][2] = "改写后的边界"
    with pytest.raises(WorkflowMetadataManifestError, match="Published"):
        validate_workflow_metadata_manifest(manifest, controls=controls)
