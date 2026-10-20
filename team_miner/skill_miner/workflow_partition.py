"""Pure, bounded method grouping; a partition is not a business-quality decision.

The host freezes complete catalog records. Model-visible method text is never
truncated, and every method has exactly one primary disposition. Shared source
support remains in the restored provenance; it does not create another Skill.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

PROTOCOL = "te-workflow-partition.v2"
OUTPUT_SCHEMA = "skill-miner.workflow-partition.v2"
CAPACITY_SCHEMA = "skill-miner.workflow-partition.capacity.v2"
MAX_METHODS = 512
MAX_CONTROLS_CHARS = 40000
MAX_OUTPUT_CHARS = 12000
REASON_MAX_CHARS = 200
METHOD_COLUMNS = ["id", "task", "boundary", "scope_id"]
SCOPE_COLUMNS = ["id", "files", "refs"]
DISPOSITION_COLUMNS = ["method_id", "destination"]


class WorkflowPartitionManifestError(ValueError):
    """Frozen host records or their hashes are inconsistent."""


class WorkflowPartitionOutputError(ValueError):
    """The model output is incomplete, ambiguous or outside the frozen contract."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _strings(value: Any, field: str, *, empty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(x, str) or not x or x.strip() != x for x in value)
            or len(set(value)) != len(value)):
        raise WorkflowPartitionManifestError(f"{field} must contain exact unique nonempty strings")
    return value


def _ordinals(value: Any, field: str) -> list[int]:
    if (not isinstance(value, list) or not value
            or any(type(x) is not int or x < 0 for x in value) or len(set(value)) != len(value)):
        raise WorkflowPartitionManifestError(f"{field} must contain unique nonnegative integer ordinals")
    return value


def _check_record(record: Any, index: int) -> None:
    field = f"records[{index}]"
    if not isinstance(record, dict) or not isinstance(record.get("item"), dict):
        raise WorkflowPartitionManifestError(f"{field} requires a complete catalog method record")
    item = record["item"]
    if item.get("kind") != "method":
        raise WorkflowPartitionManifestError(f"{field}.item must be a method")
    for key in ("item_id", "task", "boundary"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise WorkflowPartitionManifestError(f"{field}.item.{key} must be nonempty text")
    _strings([item["item_id"]], f"{field}.item.item_id")
    _strings(item.get("source_files"), f"{field}.item.source_files")
    refs = _strings(item.get("source_refs"), f"{field}.item.source_refs")
    ordinals = _ordinals(record.get("ordinals"), f"{field}.ordinals")
    bindings = record.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != set(refs):
        raise WorkflowPartitionManifestError(f"{field}.bindings must bind every original source reference exactly")
    bound = set()
    for ref, values in bindings.items():
        bound.update(_ordinals(values, f"{field}.bindings[{ref}]"))
    if bound != set(ordinals):
        raise WorkflowPartitionManifestError(f"{field}.ordinals differ from its complete source bindings")
    if "leaf_items" in record:
        _strings(record["leaf_items"], f"{field}.leaf_items")
    if "unit_id" in record:
        _strings([record["unit_id"]], f"{field}.unit_id")


def _limits(max_methods: int, max_controls_chars: int, max_output_chars: int) -> dict:
    result = {"max_methods": max_methods, "max_controls_chars": max_controls_chars,
              "max_output_chars": max_output_chars, "reason_max_chars": REASON_MAX_CHARS}
    for key, ceiling in (("max_methods", MAX_METHODS), ("max_controls_chars", MAX_CONTROLS_CHARS),
                         ("max_output_chars", MAX_OUTPUT_CHARS)):
        if type(result[key]) is not int or not 1 <= result[key] <= ceiling:
            raise ValueError(f"{key} must remain between 1 and {ceiling}")
    return result


def _assemble(records: list[dict], limits: dict) -> tuple[dict, dict]:
    methods, frozen, files, references, scopes, scope_ids = [], {}, {}, {}, {}, {}
    originals = []
    for index, record in enumerate(records):
        _check_record(record, index)
        item = record["item"]
        originals.append(item["item_id"])
        alias = f"m{index + 1:04d}"
        file_ids = []
        for source in item["source_files"]:
            files.setdefault(source, f"s{len(files) + 1}")
            file_ids.append(files[source])
        ref_ids = []
        for ref in item["source_refs"]:
            references.setdefault(ref, f"r{len(references) + 1}")
            ref_ids.append(references[ref])
        # Source membership is a set relation. Preserve each original list and
        # binding in the host record while sharing identical visible scopes.
        scope_key = (tuple(sorted(file_ids)), tuple(sorted(ref_ids)))
        if scope_key not in scope_ids:
            scope_id = f"p{len(scope_ids) + 1}"
            scope_ids[scope_key] = scope_id
            scopes[scope_id] = {"files": list(scope_key[0]), "refs": list(scope_key[1])}
        methods.append([alias, item["task"], item["boundary"], scope_ids[scope_key]])
        frozen[alias] = {"record_sha256": _sha(record), "record": copy.deepcopy(record)}
    _strings(originals, "Original method identities")
    sources = {"files": {short: full for full, short in files.items()},
               "references": {short: full for full, short in references.items()}, "scopes": scopes}
    assignment_sha = _sha({"protocol": PROTOCOL, "records": frozen, "sources": sources})
    controls = {
        "protocol": PROTOCOL, "output_schema": OUTPUT_SCHEMA, "capacity_schema": CAPACITY_SCHEMA,
        "assignment_sha256": assignment_sha, "assigned_ids": list(frozen),
        "disposition_columns": DISPOSITION_COLUMNS[:],
        "method_columns": METHOD_COLUMNS[:], "method_rows": methods,
        "source_relationships": {
            "scope_columns": SCOPE_COLUMNS[:],
            "scope_rows": [[identity, row["files"][:], row["refs"][:]] for identity, row in scopes.items()],
        },
        "limits": {"max_output_chars": limits["max_output_chars"], "reason_max_chars": REASON_MAX_CHARS},
    }
    manifest = {"protocol": PROTOCOL, "assignment_sha256": assignment_sha, "records": frozen,
                "sources": sources, "limits": copy.deepcopy(limits), "controls_sha256": _sha(controls)}
    manifest["manifest_sha256"] = _sha(manifest)
    return controls, manifest


def _output_sizes(ids: list[str], assignment_sha: str) -> dict:
    base = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": assignment_sha,
            "rows": [], "reasons": {}}
    independent = {**base, "rows": [[identity, identity] for identity in ids]}
    knowledge = {**base, "rows": [[identity, "knowledge"] for identity in ids],
                 "reasons": dict.fromkeys(ids, "")}
    capacity = {"schema_version": CAPACITY_SCHEMA, "assignment_sha256": assignment_sha,
                "input_ids": ids, "reason": "x" * REASON_MAX_CHARS}
    return {"all_independent_chars": len(_json(independent)),
            "worst_alias_structure_chars": max(len(_json(row)) for row in (independent, knowledge)),
            "capacity_signal_chars": len(_json(capacity))}


def build_workflow_partition(
    records: list[dict], *, max_methods: int = MAX_METHODS, max_controls_chars: int = MAX_CONTROLS_CHARS,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """Return ready controls/manifest, or needs_batching without clipping or merging.

    Output preflight includes the worst complete alias structure and all methods
    independent, not every optional reason at its maximum length. Actual output
    remains bounded including all text. The caller owns immutable batching.
    """
    limits = _limits(max_methods, max_controls_chars, max_output_chars)
    if not isinstance(records, list) or not records:
        raise WorkflowPartitionManifestError("A partition requires a nonempty catalog method list")
    capacity = {"method_count": len(records), "limits": limits}
    if len(records) > max_methods:
        return {"status": "needs_batching", "reason": "method_count_exceeded", "controls": None,
                "manifest": None, "capacity": capacity}
    try:
        controls, manifest = _assemble(records, limits)
        capacity.update(controls_chars=len(_json(controls)),
                        **_output_sizes(controls["assigned_ids"], controls["assignment_sha256"]))
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        if isinstance(exc, WorkflowPartitionManifestError):
            raise
        raise WorkflowPartitionManifestError("Catalog records must be complete finite JSON") from exc
    exceeded = []
    if capacity["controls_chars"] > max_controls_chars:
        exceeded.append("controls_chars_exceeded")
    if any(capacity[key] > max_output_chars for key in (
        "all_independent_chars", "worst_alias_structure_chars", "capacity_signal_chars",
    )):
        exceeded.append("output_capacity_exceeded")
    if exceeded:
        return {"status": "needs_batching", "reason": ",".join(exceeded), "controls": None,
                "manifest": None, "capacity": capacity}
    return {"status": "ready", "controls": controls, "manifest": manifest, "capacity": capacity}


def validate_workflow_partition_manifest(manifest: dict, *, controls: dict | None = None) -> None:
    """Verify frozen records, source mappings and optional published controls."""
    try:
        expected_fields = {"protocol", "assignment_sha256", "records", "sources", "limits",
                           "controls_sha256", "manifest_sha256"}
        if not isinstance(manifest, dict) or set(manifest) != expected_fields:
            raise ValueError("Unexpected manifest fields")
        frozen = manifest["records"]
        if not isinstance(frozen, dict) or not frozen or len(frozen) > MAX_METHODS:
            raise ValueError("Invalid frozen method count")
        ids = [f"m{i + 1:04d}" for i in range(len(frozen))]
        if set(frozen) != set(ids):
            raise ValueError("Frozen aliases must be a complete assignment")
        if any(set(frozen[identity]) != {"record_sha256", "record"} for identity in ids):
            raise ValueError("Unexpected frozen record fields")
        limits = manifest["limits"]
        checked_limits = _limits(limits["max_methods"], limits["max_controls_chars"], limits["max_output_chars"])
        if limits != checked_limits:
            raise ValueError("Unexpected frozen limits")
        rebuilt = build_workflow_partition([frozen[identity]["record"] for identity in ids],
                                          **{key: limits[key] for key in checked_limits if key != "reason_max_chars"})
        if rebuilt["status"] != "ready" or rebuilt["manifest"] != manifest:
            raise ValueError("Frozen record, source mapping, limits or SHA changed")
        if controls is not None and rebuilt["controls"] != controls:
            raise ValueError("Published controls differ from the frozen assignment")
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise WorkflowPartitionManifestError(str(exc)) from exc


def validate_workflow_partition_output(value: Any, manifest: dict) -> dict:
    """Reject any partial/duplicate disposition; never choose a destination for it."""
    validate_workflow_partition_manifest(manifest)
    try:
        if not isinstance(value, dict) or len(_json(value)) > manifest["limits"]["max_output_chars"]:
            raise ValueError("Partition output exceeds its serialized output limit")
        if value.get("assignment_sha256") != manifest["assignment_sha256"]:
            raise ValueError("Partition assignment_sha256 must match the frozen assignment")
        assigned = set(manifest["records"])

        def ids(raw: Any, field: str, *, empty: bool = False) -> list[str]:
            result = _strings(raw, field, empty=empty)
            if not set(result) <= assigned:
                raise ValueError(f"{field} contains an unknown method alias")
            return result

        def reason(raw: Any, field: str) -> None:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > REASON_MAX_CHARS:
                raise ValueError(f"{field} must be nonempty text of at most {REASON_MAX_CHARS} characters")

        if value.get("schema_version") == CAPACITY_SCHEMA:
            if set(value) != {"schema_version", "assignment_sha256", "input_ids", "reason"}:
                raise ValueError("Capacity output must contain only its exact declared fields")
            if ids(value["input_ids"], "input_ids") != list(manifest["records"]):
                raise ValueError("Capacity input_ids must contain the complete ordered assignment")
            reason(value["reason"], "reason")
            return copy.deepcopy(value)
        if (value.get("schema_version") != OUTPUT_SCHEMA
                or set(value) != {"schema_version", "assignment_sha256", "rows", "reasons"}):
            raise ValueError("Normal output must contain only its exact declared schema and fields")
        rows = value["rows"]
        if (not isinstance(rows, list) or len(rows) != len(assigned)
                or any(not isinstance(row, list) or len(row) != 2 or any(not isinstance(x, str) for x in row)
                       for row in rows)
                or [row[0] for row in rows] != list(manifest["records"])):
            raise ValueError("Disposition rows must contain the complete ordered assignment exactly once")
        destinations, members = dict(rows), {}
        for identity, destination in rows:
            if destination in {"knowledge", "deferred"}:
                continue
            if destination not in assigned:
                raise ValueError("Disposition destination contains an unknown method alias")
            if destinations[destination] != destination:
                raise ValueError("Every chosen parent must explicitly self-map; chains and cycles are forbidden")
            members.setdefault(destination, []).append(identity)
        reasons = value["reasons"]
        expected_reasons = {parent for parent, group in members.items() if len(group) > 1}
        expected_reasons.update(identity for identity, destination in rows if destination in {"knowledge", "deferred"})
        if not isinstance(reasons, dict) or set(reasons) != expected_reasons:
            raise ValueError("Reasons must exactly cover grouped parents and knowledge/deferred method IDs")
        for identity, explanation in reasons.items():
            reason(explanation, f"reasons[{identity}]")
        return copy.deepcopy(value)
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise WorkflowPartitionOutputError(str(exc)) from exc


def _provenance(records: list[dict]) -> dict:
    result: dict = {"source_files": [], "source_refs": [], "ordinals": [], "bindings": {},
                    "unit_ids": [], "leaf_items": []}
    for record in records:
        for key in ("source_files", "source_refs"):
            result[key].extend(x for x in record["item"][key] if x not in result[key])
        for ref, values in record["bindings"].items():
            result["bindings"][ref] = sorted(set(result["bindings"].get(ref, [])) | set(values))
        result["ordinals"] = sorted(set(result["ordinals"]) | set(record["ordinals"]))
        for key, values in (("unit_ids", [record["unit_id"]] if "unit_id" in record else []),
                            ("leaf_items", record.get("leaf_items", [record["item"]["item_id"]]))):
            result[key].extend(x for x in values if x not in result[key])
    return result


def restore_workflow_partition(value: Any, manifest: dict) -> dict:
    """Restore complete records and union provenance; never synthesize a parent."""
    checked = validate_workflow_partition_output(value, manifest)
    if checked["schema_version"] == CAPACITY_SCHEMA:
        raise WorkflowPartitionOutputError("Capacity requires host batching; it has no accepted dispositions")

    def restore(parent: str, members: list[str], reason: str | None = None) -> dict:
        records = [copy.deepcopy(manifest["records"][identity]["record"]) for identity in members]
        result = {"parent_id": parent, "member_ids": members[:],
                  "parent_record": copy.deepcopy(manifest["records"][parent]["record"]),
                  "records": records, "provenance": _provenance(records)}
        if reason is not None:
            result["reason"] = reason
        return result

    result = {"schema_version": OUTPUT_SCHEMA, "assignment_sha256": checked["assignment_sha256"],
              "groups": [], "independent": [], "knowledge": [], "deferred": []}
    members = {}
    for identity, destination in checked["rows"]:
        if destination in {"knowledge", "deferred"}:
            result[destination].append(restore(identity, [identity], checked["reasons"][identity]))
        else:
            members.setdefault(destination, []).append(identity)
    for parent, group in members.items():
        if len(group) == 1:
            result["independent"].append(restore(parent, group))
        else:
            result["groups"].append(restore(parent, group, checked["reasons"][parent]))
    return result
