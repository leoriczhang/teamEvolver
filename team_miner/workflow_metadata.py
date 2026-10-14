"""Pure frozen workflow metadata assignments; provenance is restored only by TE."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from . import bounded_compile_plan as planning
from .output_language import _natural_text, validate_artifact_headings
from .workflow_partition import MAX_METHODS, _check_record, _provenance

PROTOCOL = "te-workflow-metadata.v1"
OUTPUT_SCHEMA = "skill-miner.workflow-metadata.v1"
MAX_CONTROLS_CHARS = 40000
MAX_OUTPUT_CHARS = 12000
CANDIDATE_FIELDS = frozenset({"name", "task", "trigger", "inputs", "outputs", "boundary", "rationale"})
_RULE = (
    "Describe only this complete assigned workflow. Method indexes and source relationships are planning "
    "context, not Evidence. The host restores every member's exact source bindings and original method identity. "
    "Do not repartition, invent a parent, widen a narrow method or turn static facts into procedures."
)


class WorkflowMetadataManifestError(ValueError):
    """Frozen grouping, complete source lineage or published controls changed."""


class WorkflowMetadataCapacityError(ValueError):
    """The complete workflow assignment does not fit; no method text was clipped."""


class WorkflowMetadataOutputError(ValueError):
    """Model metadata is outside its exact single-workflow contract."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _limits(max_controls_chars: int, max_output_chars: int) -> dict:
    limits = {"max_controls_chars": max_controls_chars, "max_output_chars": max_output_chars}
    for key, ceiling in (("max_controls_chars", MAX_CONTROLS_CHARS), ("max_output_chars", MAX_OUTPUT_CHARS)):
        if type(limits[key]) is not int or not 1 <= limits[key] <= ceiling:
            raise ValueError(f"{key} must remain between 1 and {ceiling}")
    return limits


def _checked_group(group: Any) -> dict:
    """Accept the common restored shape for either a group or an independent method."""
    fields = {"parent_id", "member_ids", "parent_record", "records", "provenance"}
    if (not isinstance(group, dict) or not fields <= set(group)
            or set(group) - fields - {"reason"}):
        raise WorkflowMetadataManifestError("A workflow needs its complete restored group and provenance")
    ids, records = group["member_ids"], group["records"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_METHODS
            or any(not isinstance(identity, str) or not identity or identity.strip() != identity for identity in ids)
            or len(set(ids)) != len(ids) or not isinstance(records, list) or len(records) != len(ids)):
        raise WorkflowMetadataManifestError("Workflow members require unique IDs and one complete record each")
    if not isinstance(group["parent_id"], str) or group["parent_id"] not in ids:
        raise WorkflowMetadataManifestError("The complete parent must be an assigned group member")
    if "reason" in group and (
        not isinstance(group["reason"], str) or not group["reason"].strip() or len(group["reason"]) > 200
    ):
        raise WorkflowMetadataManifestError("A grouping reason must be nonempty and at most 200 characters")
    original_ids, bindings = set(), {}
    for index, record in enumerate(records):
        _check_record(record, index)
        identity = record["item"]["item_id"]
        if identity in original_ids:
            raise WorkflowMetadataManifestError("Workflow members cannot repeat an original method identity")
        original_ids.add(identity)
        for ref, ordinals in record["bindings"].items():
            if ref in bindings and bindings[ref] != sorted(ordinals):
                raise WorkflowMetadataManifestError("One original source reference has conflicting exact bindings")
            bindings[ref] = sorted(ordinals)
    if group["parent_record"] != records[ids.index(group["parent_id"])]:
        raise WorkflowMetadataManifestError("The complete parent record differs from its assigned member")
    if group["provenance"] != _provenance(records):
        raise WorkflowMetadataManifestError("Workflow provenance must equal the union of all complete members")
    return copy.deepcopy(group)


def _assemble(group: dict, limits: dict) -> tuple[dict, dict]:
    frozen = _checked_group(group)
    methods, files, refs, scopes, scope_ids = [], {}, {}, {}, {}
    for identity, record in zip(frozen["member_ids"], frozen["records"]):
        item = record["item"]
        file_ids, ref_ids = [], []
        for uri in item["source_files"]:
            files.setdefault(uri, f"s{len(files) + 1}")
            file_ids.append(files[uri])
        for ref in item["source_refs"]:
            refs.setdefault(ref, f"r{len(refs) + 1}")
            short = refs[ref]
            ref_ids.append(short)
        scope_key = _json({"files": sorted(file_ids), "refs": sorted(ref_ids)})
        if scope_key not in scope_ids:
            scope_ids[scope_key] = f"p{len(scopes) + 1}"
            scopes[scope_ids[scope_key]] = {"files": sorted(file_ids), "refs": sorted(ref_ids)}
        methods.append([identity, item["task"], item["boundary"], scope_ids[scope_key]])
    assignment_sha = _sha({"protocol": PROTOCOL, "group": frozen, "limits": limits})
    controls = {
        "protocol": PROTOCOL, "output_schema": OUTPUT_SCHEMA, "assignment_sha256": assignment_sha,
        "parent_id": frozen["parent_id"], "member_ids": frozen["member_ids"][:],
        "method_columns": ["id", "task", "boundary", "scope_id"], "method_rows": methods,
        "source_relationships": {"scopes": scopes},
        "limits": {"max_output_chars": limits["max_output_chars"], "deferred_reason_max_chars": 400},
        "rule": _RULE,
    }
    manifest = {
        "protocol": PROTOCOL, "assignment_sha256": assignment_sha, "group": frozen,
        "group_sha256": _sha(frozen),
        "member_sha256": {identity: _sha(record)
                          for identity, record in zip(frozen["member_ids"], frozen["records"])},
        "sources": {"files": {short: full for full, short in files.items()},
                    "references": {short: full for full, short in refs.items()},
                    "scopes": copy.deepcopy(scopes)},
        "limits": copy.deepcopy(limits), "controls_sha256": _sha(controls),
    }
    manifest["manifest_sha256"] = _sha(manifest)
    return controls, manifest


def build_workflow_metadata(
    group: dict, *, max_controls_chars: int = MAX_CONTROLS_CHARS, max_output_chars: int = MAX_OUTPUT_CHARS,
) -> tuple[dict, dict]:
    """Freeze a complete workflow; raise on capacity instead of truncating any member.

    The caller must also measure the entire stage packet, compiler and instruction
    against the transport envelope. This helper owns only its explicit controls budget.
    """
    try:
        limits = _limits(max_controls_chars, max_output_chars)
        controls, manifest = _assemble(group, limits)
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        if isinstance(exc, WorkflowMetadataManifestError):
            raise
        raise WorkflowMetadataManifestError(str(exc)) from exc
    if len(_json(controls)) > max_controls_chars:
        raise WorkflowMetadataCapacityError("The complete workflow metadata controls exceed the frozen input budget")
    return controls, manifest


def validate_workflow_metadata_manifest(manifest: Any, *, controls: dict | None = None) -> None:
    """Rebuild all hashes and source relationships, optionally checking published controls."""
    try:
        fields = {"protocol", "assignment_sha256", "group", "group_sha256", "member_sha256", "sources", "limits",
                  "controls_sha256", "manifest_sha256"}
        if not isinstance(manifest, dict) or set(manifest) != fields:
            raise ValueError("Unexpected frozen workflow metadata manifest fields")
        limits = manifest["limits"]
        if not isinstance(limits, dict) or set(limits) != {"max_controls_chars", "max_output_chars"}:
            raise ValueError("Unexpected frozen workflow metadata limits")
        expected_controls, expected = build_workflow_metadata(manifest["group"], **limits)
        if expected != manifest:
            raise ValueError("Frozen workflow metadata assignment, records, source mappings or hashes changed")
        if controls is not None and controls != expected_controls:
            raise ValueError("Published workflow metadata controls differ from the frozen assignment")
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise WorkflowMetadataManifestError(str(exc)) from exc


def validate_workflow_metadata_output(value: Any, manifest: dict, *, language_policy: dict | None = None) -> dict:
    """Validate a single Candidate or deferral; this does not establish business correctness."""
    validate_workflow_metadata_manifest(manifest)
    try:
        if not isinstance(value, dict) or len(_json(value)) > manifest["limits"]["max_output_chars"]:
            raise ValueError("Workflow metadata output must be one bounded JSON object")
        if value.get("schema_version") != OUTPUT_SCHEMA:
            raise ValueError("Workflow metadata output uses an unexpected schema")
        if value.get("assignment_sha256") != manifest["assignment_sha256"]:
            raise ValueError("Workflow metadata output belongs to another frozen assignment")
        refs = manifest["group"]["provenance"]["source_refs"]
        plan = {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []}
        if set(value) == {"schema_version", "assignment_sha256", "candidate"}:
            candidate = value["candidate"]
            if not isinstance(candidate, dict) or set(candidate) != CANDIDATE_FIELDS:
                raise ValueError("One Candidate requires exactly its seven canonical metadata fields")
            plan["skills"] = [{**copy.deepcopy(candidate), "source_refs": refs[:]}]
        elif set(value) == {"schema_version", "assignment_sha256", "deferred"}:
            row = value["deferred"]
            if not isinstance(row, dict) or set(row) != {"reason"}:
                raise ValueError("Workflow deferral requires only its explicit reason")
            plan["deferred"] = [{**copy.deepcopy(row), "source_refs": refs[:]}]
        else:
            raise ValueError("Return exactly one Candidate or one deferral without model-written provenance")
        # Host-expanded references may be larger than the model response. Every
        # business-field constraint still uses the existing public plan validator.
        planning.validate_plan(plan, source_refs=refs, bounded=False)
        if language_policy is not None:
            if not isinstance(language_policy, dict):
                raise ValueError("Workflow metadata language policy must be an object")
            descriptions = ([value["candidate"][field] for field in ("task", "trigger", "boundary", "rationale")]
                            if "candidate" in value else [value["deferred"]["reason"]])
            for description in descriptions:
                validate_artifact_headings("", language_policy, title=description)
            if "candidate" in value:
                # Inputs and outputs may be machine keys, paths, code or numeric
                # values. Check only the remaining generated natural language;
                # preserve the actual value and its technical tokens unchanged.
                for description in value["candidate"]["inputs"] + value["candidate"]["outputs"]:
                    natural = _natural_text(description)
                    if any(character.isalpha() for character in natural):
                        validate_artifact_headings("", language_policy, title=natural)
        return copy.deepcopy(value)
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise WorkflowMetadataOutputError(str(exc)) from exc


def restore_workflow_metadata(value: Any, manifest: dict, *, language_policy: dict | None = None) -> dict:
    """Restore all original method IDs and exact sources for either accepted outcome."""
    checked = validate_workflow_metadata_output(value, manifest, language_policy=language_policy)
    group = manifest["group"]
    field = "candidate" if "candidate" in checked else "deferred"
    checked[field]["derived_from"] = [record["item"]["item_id"] for record in group["records"]]
    checked[field]["source_refs"] = group["provenance"]["source_refs"][:]
    checked["provenance"] = copy.deepcopy(group["provenance"])
    return checked
