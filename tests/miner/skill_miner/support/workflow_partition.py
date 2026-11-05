# ruff: noqa: F401
"""Lossless method partition contracts; no model, filesystem or network work."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner.workflow_partition import (
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
