# ruff: noqa: F401
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
