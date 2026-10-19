"""Bounded direct workflow controls; indexes select original ranges but are never Evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import bounded_compile_plan as planning
from .source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL_V3, prepare_source_routing

DIRECT_WORKFLOW_PROTOCOL = "te-direct-workflow-planning.v1"
SELECTION_RECOVERY_PROTOCOL = "te-selection-recovery.v1"
LOCAL_SELECTION_RECOVERY_PROTOCOL = "te-selection-recovery.v2"
MAX_DIRECT_METHODS = 24
CAPACITY_SCHEMA = "skill-miner.selection.capacity.v1"
MAX_RELATED_CONTEXT_CHARS = 8000


class SelectionOutputValidationError(ValueError):
    """Invalid model dispositions, distinct from damaged frozen inputs or host state."""


class FrozenPlanningIntegrityError(ValueError):
    """The local frozen assignment is damaged; a new model call cannot repair it."""


def validate_capacity(
    value: Any, input_ids: list[str], source_refs: list[str], *, max_reason_chars: int = 1000,
    _max_output_chars: int = 12000,
) -> bool:
    """Validate a split signal, not a disposition. The caller checks the actual method-window size.

    Multiple methods may share one container ID, so container count cannot decide
    whether splitting is possible. A normal selection response returns False.
    """
    if not isinstance(value, dict) or value.get("schema_version") != CAPACITY_SCHEMA:
        return False
    planning._bounded_output(value, _max_output_chars=_max_output_chars)
    if set(value) != {"schema_version", "input_ids", "source_refs", "reason"}:
        raise ValueError("A capacity signal requires exactly its schema, input IDs, source references and reason")
    for expected, label in ((input_ids, "input IDs"), (source_refs, "source references")):
        if (not isinstance(expected, list) or not expected
                or any(not isinstance(item, str) or not item.strip() for item in expected)
                or len(expected) != len(set(expected))):
            raise ValueError(f"Capacity assignment requires exact unique {label}")
    planning._coverage(value["input_ids"], input_ids)
    planning._coverage(value["source_refs"], source_refs)
    if type(max_reason_chars) is not int or not 1 <= max_reason_chars <= planning.MAX_OUTPUT_CHARS:
        raise ValueError("Capacity diagnostic limit must remain within the complete output budget")
    planning._text(value["reason"], max_reason_chars, "capacity reason")
    return True


def _read(root: Path, path: Path) -> dict:
    """Only read this round's private metadata, never a symlink into another job."""
    root, path = root.absolute(), path.absolute()
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Direct planning metadata must remain inside its own frozen round")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Direct planning metadata cannot use symbolic links")
        current = current.parent
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Direct planning metadata must be an object")
    return value


def _ordinals(value: Any, count: int) -> set[int]:
    if (not isinstance(value, list) or not value
            or any(type(index) is not int or not 0 <= index < count for index in value)
            or len(value) != len(set(value))):
        raise ValueError("Readonly context requires exact frozen original fragment ordinals")
    return set(value)


def _explicit_shared(root: Path, state: dict, methods: set[int]) -> tuple[set[int], bool]:
    if state.get("efficiency_protocol") != EFFICIENCY_PROTOCOL:
        return set(), False
    for index in range(int(state["unit_count"])):
        _read(root, root / "units/0" / f"{index:08d}.json")
    path = root / "source-routing/context.json"
    if path.exists():
        _read(root, path)
    routing = prepare_source_routing(root, state)
    if not routing["coverage_complete"]:
        return set(), False
    if state.get("source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3:
        from .source_scope import candidate_context_ordinals

        return set(candidate_context_ordinals(root, sorted(methods), routing)), True
    # An uncertain-file fallback is safe for raw writers, but it is not an
    # explicit assertion that every fact in that file is shared planning context.
    shared = set()
    for index in range(int(state["unit_count"])):
        unit = _read(root, root / "units/0" / f"{index:08d}.json")
        shared.update(unit["context_ordinals"])
    return shared, True


def related_context(root: Path, state: dict, records: list[dict]) -> dict:
    """Stream exact-fragment and verified shared notes into an 8K readonly projection.

    Notes cannot be consumed in derived_from. Omitted notes stay in the catalog
    and their original text stays in frozen chunks for later writer/Case checks.
    """
    if state.get("direct_workflow_protocol") != DIRECT_WORKFLOW_PROTOCOL:
        raise ValueError("Readonly direct context requires its exact frozen protocol")
    if not records or len(records) > MAX_DIRECT_METHODS:
        raise ValueError("Direct planning requires one through 24 assigned methods")
    count = state["chunk_count"]
    if type(count) is not int or count < 1:
        raise ValueError("Direct planning needs the frozen original chunk count")
    methods = set()
    for record in records:
        if record["item"].get("kind") != "method":
            raise ValueError("Direct planning assignments must remain typed methods")
        methods.update(_ordinals(record["ordinals"], count))
    exact_units = state.get("source_unit_protocol") == "te-exact-source-units.v1"
    method_units = set()
    if exact_units:
        from .source_unit_review import prepare_routing

        routing = prepare_routing(root, state)

        def record_units(record):
            bindings = record.get("source_unit_bindings")
            if not isinstance(bindings, dict):
                raise ValueError("Exact readonly context lost its original unit membership")
            return {identity for ref in record["item"]["source_refs"] for identity in bindings[ref]}

        method_units = {identity for record in records for identity in record_units(record)}
        shared, verified = set(routing["shared_unit_ids"]), routing["coverage_complete"]
        for group in routing["owned_context_groups"]:
            if method_units.intersection(group["method_unit_ids"]):
                shared.update(group["context_unit_ids"])
        for group in routing["uncertain_unit_groups"]:
            if method_units.intersection(group):
                shared.update(group)
    else:
        shared, verified = _explicit_shared(root, state, methods)
    audit = {
        "budget_chars": MAX_RELATED_CONTEXT_CHARS,
        "shared_context_verified": verified,
        "matched_notes": 0,
        "selected_before_authority_budget": 0,
        "omitted_notes": 0,
        "original_fragments_preserved": True,
        "projection_is_evidence": False,
    }
    result = {
        "related_facts": [], "provisional_gaps": [], "context_omitted": False,
        "readonly_context_audit": audit,
        "readonly_context_rule": "Only assigned items are dispositions. These notes are readonly routing context, "
        "not Evidence; never consume their IDs. Omitted notes do not establish an Evidence gap. "
        "Verify business rules against the frozen original ranges in subsequent writers and Cases.",
    }
    counts = state["catalog_counts"]
    # Two passes prioritize method-local notes without retaining a global catalog in RAM.
    for priority in ("method", "shared"):
        for kind, field, text_field in (("fact", "related_facts", "text"), ("gap", "provisional_gaps", "reason")):
            for index in range(int(counts.get(kind, 0))):
                record = _read(root, root / "catalog" / kind / f"{index:08d}.json")
                ordinals = _ordinals(record["ordinals"], count)
                membership = record_units(record) if exact_units else ordinals
                exact = bool(membership & (method_units if exact_units else methods))
                if not (exact if priority == "method" else not exact and bool(membership & shared)):
                    continue
                item = record["item"]
                if item.get("kind") != kind:
                    raise ValueError("Readonly catalog kind changed from its frozen disposition")
                planning._text(item.get(text_field), 1000, text_field)
                fragment_ids = []
                for ordinal in sorted(ordinals):
                    metadata = _read(root, root / "chunk-metadata" / f"{ordinal:08d}.json")
                    identity = metadata.get("id")
                    if not isinstance(identity, str) or not identity.startswith("src-"):
                        raise ValueError("Readonly context needs exact original source fragment IDs")
                    fragment_ids.append(identity)
                note = {text_field: item[text_field], "source_fragment_ids": fragment_ids,
                        "readonly": True, "relation": "method_fragment" if exact else "verified_shared_constraint"}
                audit["matched_notes"] += 1
                audit["selected_before_authority_budget"] += 1
                result[field].append(note)
                # Reserve enough for final counters/flags. Never clip the note's provenance.
                if len(planning._json(result)) + 64 > MAX_RELATED_CONTEXT_CHARS:
                    result[field].pop()
                    audit["selected_before_authority_budget"] -= 1
                    audit["omitted_notes"] += 1
                    result["context_omitted"] = True
    if len(planning._json(result)) > MAX_RELATED_CONTEXT_CHARS:
        raise ValueError("Readonly context audit exceeded its bounded projection")
    return result
