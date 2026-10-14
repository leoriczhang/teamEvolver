"""Exact-unit extraction packets and lineage within the existing independent file Map stage."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from . import bounded_compile_plan as planning
from .source_units import PROTOCOL, SourceUnitCapacityError, _json, _sha, load_source_unit, load_source_units

EXACT_UNIT_GUIDANCE = (
    "This frozen packet uses te-exact-source-units.v1. Each packet.evidence entry is one complete "
    "original Markdown table row, paragraph, heading, table header or code block with exact offsets "
    "and SHA. Its ordinal is a SOURCE UNIT ordinal, NOT an archived transport chunk ordinal; "
    "ordinals lists its original archived chunks. The metadata is host bookkeeping, not business "
    "Evidence. source_refs and context_refs explicitly select the exact unit handles in "
    "derived_controls.reference_map. Do not cite a whole file URI, a bare numeric row ID, an "
    "unassigned handle or merely a nearby row. A table header defines columns, not the applicability "
    "of all other rows. Structural context_unit_ids are not automatically shared business rules. "
    "Every assigned unit must remain covered by methods, facts or deferred, including contextual "
    "headings and separators; factual structure may be represented together without inventing a "
    "method. Select all and only complete units supporting each method's actions and conditions; "
    "different rows sharing one original chunk must not acquire each other's personas, amounts, "
    "exceptions or ordering. Preserve source contradictions as unresolved source issues, never "
    "silently repair original figures or names. The output remains exactly skill-miner.index.v2: "
    "do not output new fields, offsets, SHA, URIs or host source_unit_bindings. Full archived source "
    "units and their identity are recovered by TE from the selected handles."
)


def render_source_unit_extract_compiler(base_body: str) -> str:
    from .compile_strategy import (
        _append_fresh_synthesize_guidance,
        render_input_clarity_extract_batch_compiler_v3,
    )

    return _append_fresh_synthesize_guidance(
        render_input_clarity_extract_batch_compiler_v3(base_body), EXACT_UNIT_GUIDANCE,
    )


def build_source_unit_packet(
    root: Path, context: dict, language_policy: dict, *, repair_error: str = "", max_chars: int = 32000,
) -> dict:
    units = load_source_units(root, context["source_unit_ids"])
    ids = [unit["id"] for unit in units]
    if ids != context["input_ids"] or context["source_refs"] != [f"u{i + 1}" for i in range(len(ids))]:
        raise ValueError("Exact source-unit packet changed its assigned aliases or order")
    controls = {
        "source_unit_protocol": PROTOCOL,
        "assigned_unit_ids": context["assigned_unit_ids"],
        "allowed_source_refs": context["source_refs"],
        "reference_map": dict(zip(context["source_refs"], ids, strict=True)),
    }
    if repair_error:
        controls["repair_error"] = repair_error
    return planning.build_stage_packet(
        "extract", f"index-{context['unit_index']:08d}.json", evidence_chunks=units,
        controls=controls, language_policy=language_policy, max_chars=max_chars,
    )


def next_source_unit_assignment(
    root: Path, *, cursor: int, count: int, unit_index: int, language_policy: dict,
    max_packet_chars: int = 30000, max_units: int = 16,
) -> dict:
    """Budget full units including structural context; never split or omit an assigned unit."""
    if (type(cursor) is not int or type(count) is not int or not 0 <= cursor < count
            or type(max_units) is not int or not 1 <= max_units <= 16 or max_packet_chars <= 0):
        raise ValueError("Exact extraction requires a bounded source-unit assignment")
    accepted = None
    for after in range(cursor + 1, min(count, cursor + max_units) + 1):
        primary = load_source_units(root, list(range(cursor, after)))
        units = load_source_units(root, [unit["id"] for unit in primary], include_context=True)
        ids = [unit["id"] for unit in units]
        context = {
            "source_unit_protocol": PROTOCOL, "before": cursor, "after": after, "unit_index": unit_index,
            "assigned_unit_ids": [unit["id"] for unit in primary], "source_unit_ids": ids, "input_ids": ids,
            "source_refs": [f"u{i + 1}" for i in range(len(ids))],
            "ordinals": sorted({ordinal for unit in units for ordinal in unit["ordinals"]}),
        }
        try:
            build_source_unit_packet(root, context, language_policy, max_chars=max_packet_chars)
        except ValueError as exc:
            if "exceeds its bounded input budget" not in str(exc):
                raise
            if accepted is None:
                unit = load_source_unit(root, cursor)
                raise SourceUnitCapacityError(
                    "Complete source-unit capacity exceeded: " + unit["id"] + " (" + unit["kind"] + ", "
                    + str(len(unit["content"])) + " original characters). The full parent and original chunks "
                    "remain archived; this is an input-capacity limit, not missing Evidence."
                ) from exc
            break
        accepted = context
    assert accepted is not None
    return accepted


def verify_source_unit_context(root: Path, context: dict) -> None:
    """Bind contiguous primary unit coverage and all context to exact frozen source identities."""
    if context.get("source_unit_protocol") != PROTOCOL:
        raise ValueError("Exact source-unit assignment lost its frozen protocol")
    primary = load_source_units(root, list(range(context["before"], context["after"])))
    units = load_source_units(root, [unit["id"] for unit in primary], include_context=True)
    if (context["assigned_unit_ids"] != [unit["id"] for unit in primary]
            or context["source_unit_ids"] != [unit["id"] for unit in units]
            or context["input_ids"] != context["source_unit_ids"]
            or context["source_refs"] != [f"u{i + 1}" for i in range(len(units))]
            or context["ordinals"] != sorted({ordinal for unit in units for ordinal in unit["ordinals"]})):
        raise ValueError("Exact source-unit assignment changed its original coverage or structural context")


def bind_source_unit_index(root: Path, value: dict, packet: dict, context: dict, identity: str) -> dict:
    """Validate model selection; restore precise unit sets and archived chunk lineage in the host."""
    from .ov_staged_orchestration import _alias, _canonical_extract_references

    verify_source_unit_context(root, context)
    reference_map = packet["derived_controls"]["reference_map"]
    allowed = set(context["source_refs"]) | set(context["input_ids"])
    if not isinstance(value, dict):
        raise ValueError("Exact source-unit extraction must return an index object")
    selected_refs = [value.get("context_refs", [])]
    for group in ("methods", "facts", "deferred"):
        if isinstance(value.get(group), list):
            selected_refs.extend(row.get("source_refs", []) for row in value[group] if isinstance(row, dict))
    if any(not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
           for refs in selected_refs):
        raise ValueError("Exact extraction requires assigned source-unit handles, not whole-file or neighbouring IDs")
    canonical = _canonical_extract_references(
        value, input_ids=context["input_ids"], source_refs=context["source_refs"], reference_map=reference_map,
    )
    canonical = planning.validate_index(
        canonical, input_ids=context["input_ids"], source_refs=context["source_refs"], routing=True,
    )
    units = {unit["id"]: unit for unit in load_source_units(root, context["source_unit_ids"])}
    bindings, source_unit_bindings = {}, {}

    def scope(refs: list[str]) -> list[str]:
        if not refs:
            return []
        ids = sorted({reference_map[ref] for ref in refs})
        alias = "scope-" + _sha("source-unit-scope:" + _json(ids))[:24]
        source_unit_bindings[alias] = ids
        bindings[alias] = sorted({ordinal for unit_id in ids for ordinal in units[unit_id]["ordinals"]})
        return [alias]

    projected = deepcopy(canonical)
    for group in ("methods", "facts", "deferred"):
        for row in projected[group]:
            row["source_refs"] = scope(row["source_refs"])
    projected["context_refs"] = scope(canonical["context_refs"])
    return {
        "id": _alias(identity), "value": projected, "bindings": bindings,
        "source_unit_protocol": PROTOCOL, "source_unit_bindings": source_unit_bindings,
        "assigned_unit_ids": list(context["assigned_unit_ids"]),
        "ordinals": sorted({ordinal for values in bindings.values() for ordinal in values}),
        "context_ordinals": sorted({ordinal for ref in projected["context_refs"] for ordinal in bindings[ref]}),
        "context_uncertain": canonical["context_uncertain"],
    }
