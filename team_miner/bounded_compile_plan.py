"""Pure TE staging: lossless Evidence packets, bounded outputs and canonical assembly.

Only original text in packet.evidence is Evidence. Generated indexes and prior
candidates are controls; their aliases never become final Evidence citations.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from team_miner import benchmark_format
from team_skills.library.frontmatter import render_skill_md

from .compile_strategy import stage_compiler_body as stage_compiler_body

PACKET_SCHEMA = "te-staged-compile.packet.v1"
INDEX_SCHEMA = "skill-miner.index.v1"
INDEX_SCHEMA_V2 = "skill-miner.index.v2"
PLAN_SCHEMA = "skill-miner.plan.v1"
MERGE_SCHEMA = "skill-miner.merge-index.v1"
SELECTION_SCHEMA = "skill-miner.selection.v1"
SELECTION_SCHEMA_V2 = "skill-miner.selection.v2"
SELECTION_SCHEMA_V3 = "skill-miner.selection.v3"
SECTION_SCHEMA = "skill-miner.section.v1"
SECTION_SKIP_SCHEMA = "skill-miner.section-skip.v1"
DEFERRED_SCHEMA = "skill-miner.deferred.v1"
MAX_OUTPUT_CHARS = 12000
PLANNING_COVERAGE_SCHEMA = "te-planning-coverage-diagnostic.v1"
MAX_COVERAGE_DIAGNOSTIC_IDS = 24
DISCLOSURE = dict(benchmark_format.PROGRESSIVE_DISCLOSURE)
CASE_KEYS = {"case_id", "query", "skill_ids", "checks", "materials", "provenance", "replay", "metadata"}
CASE_DIMENSIONS = (
    "An ordinary supported execution with complete supplied inputs.",
    "Another supported input or execution path.",
    "An explicitly documented applicability boundary.",
    "An explicitly documented exception to the ordinary path.",
    "A required input or material is missing.",
    "Supplied inputs are ambiguous or conflict with one another.",
    "An explicitly documented exclusion or refusal condition.",
    "A misleading assertion that must be checked against the original Evidence.",
    "Another documented applicability condition.",
    "A documented numerical or categorical condition.",
    "A documented ordering or verification requirement.",
    "Another distinct scenario supported by the assigned original Evidence.",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_case_query(query: str) -> str:
    """Compare scenario text without changing its stored content or business facts."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Case query must be nonempty text")
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def _chunk(text: str, uri: str, start: int, end: int, source_hash: str) -> dict:
    raw = text[start:end]
    digest = _hash(raw)
    return {
        "id": "src-" + _hash(_json([uri, start, end, digest]))[:24],
        "source_uri": uri,
        "start_char": start,
        "end_char": end,
        "sha256": digest,
        "source_sha256": source_hash,
        "content": raw,
    }


def source_chunks(text: str, source_uri: str, *, max_chars: int = 12000) -> list[dict]:
    """Split exact Unicode text by its serialized size, without changing or omitting a character."""
    if not isinstance(text, str) or not isinstance(source_uri, str) or not source_uri.startswith("viking://"):
        raise ValueError("Source text and its exact Viking URI are required")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 512:
        raise ValueError("Source chunk budget must be at least 512 characters")
    source_hash = _hash(text)
    chunks, start = [], 0
    if not text:
        empty = _chunk(text, source_uri, 0, 0, source_hash)
        if len(_json(empty)) > max_chars:
            raise ValueError("Source URI metadata exceeds the chunk budget")
        return [empty]
    while start < len(text):
        low, high = 0, min(max_chars, len(text) - start)
        while low < high:
            width = (low + high + 1) // 2
            if len(_json(_chunk(text, source_uri, start, start + width, source_hash))) <= max_chars:
                low = width
            else:
                high = width - 1
        if low == 0:
            raise ValueError("Source URI metadata exceeds the chunk budget")
        chunks.append(_chunk(text, source_uri, start, start + low, source_hash))
        start += low
    return chunks


def validate_chunks(chunks: list[dict]) -> None:
    seen = set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or chunk.get("id") in seen:
            raise ValueError("Evidence chunk IDs must be unique")
        raw = chunk.get("content")
        start, end = chunk.get("start_char"), chunk.get("end_char")
        if (
            not isinstance(raw, str)
            or chunk.get("sha256") != _hash(raw)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start <= end
            or end - start != len(raw)
            or not isinstance(chunk.get("source_uri"), str)
            or not chunk["source_uri"].startswith("viking://")
        ):
            raise ValueError("Evidence chunk content, offsets or hash is invalid")
        expected = "src-" + _hash(_json([chunk["source_uri"], start, end, chunk["sha256"]]))[:24]
        if chunk["id"] != expected:
            raise ValueError("Evidence chunk identity does not match its exact source and offsets")
        seen.add(chunk["id"])


def pack_chunks(chunks: list[dict], *, max_packet_chars: int = 24000, max_chunks: int = 8) -> list[list[dict]]:
    """Bound actual JSON and chunk count; never silently drop an indivisible chunk."""
    validate_chunks(chunks)
    if max_packet_chars < 512 or max_chunks < 1:
        raise ValueError("Invalid packet budget")
    groups, current = [], []
    for chunk in chunks:
        if len(_json({"evidence": [chunk]})) > max_packet_chars:
            raise ValueError("One chunk exceeds the packet budget; create smaller lossless source chunks")
        if current and (len(current) >= max_chunks or len(_json({"evidence": [*current, chunk]})) > max_packet_chars):
            groups.append(current)
            current = []
        current.append(chunk)
    if current:
        groups.append(current)
    return groups


def build_stage_packet(
    stage: str,
    output_path: str,
    *,
    evidence_chunks: list[dict] | tuple = (),
    controls: Any = None,
    revision_context: str = "",
    language_policy: dict | None = None,
    max_chars: int = 32000,
) -> dict:
    """Create one task's exact raw Evidence and explicitly nonfactual planning context."""
    from pathlib import PurePosixPath

    validate_chunks(list(evidence_chunks))
    path = PurePosixPath(output_path)
    if not output_path or path.is_absolute() or ".." in path.parts or "\\" in output_path:
        raise ValueError("Stage output path must be a safe relative path")
    packet = {
        "schema_version": PACKET_SCHEMA,
        "stage": stage,
        "output_path": output_path,
        "evidence": deepcopy(list(evidence_chunks)),
        "input_ids": [chunk["id"] for chunk in evidence_chunks],
        "derived_controls": deepcopy(controls),
        "derived_controls_are_evidence": False,
        "revision_context": revision_context,
        "revision_context_is_evidence": False,
        "output_language_policy": deepcopy(language_policy),
    }
    if len(_json(packet)) > max_chars:
        raise ValueError("Stage packet exceeds its bounded input budget; split it without dropping Evidence")
    return packet


def _text(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters")
    return value


def _refs(value: Any, allowed: list[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(ref, str) for ref in value)
        or len(set(value)) != len(value)
        or not set(value) <= set(allowed)
    ):
        raise ValueError("References must be exact nonempty assigned references")
    return value


def _coverage(value: Any, expected: list[str]) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
        or set(value) != set(expected)
    ):
        raise ValueError("Stage input_ids must cover every exact assigned input ID")


def planning_coverage_diagnostics(value: Any, assigned_item_ids: list[str]) -> dict | None:
    """Describe exact disposition defects, without proposing or accepting a disposition.

    All assigned identities remain in the packet. Diagnostic lists are bounded;
    an unusually large or malformed unknown identity is omitted with a count,
    never shortened into a different identity.
    """
    if (not isinstance(assigned_item_ids, list)
            or any(not isinstance(identity, str) or not identity for identity in assigned_item_ids)
            or len(assigned_item_ids) != len(set(assigned_item_ids))):
        raise ValueError("Planning coverage needs unique exact assigned item IDs")
    if not isinstance(value, dict):
        return None
    groups = ("skills", "knowledge", "deferred", "resolved") if "skills" in value else (
        "methods", "facts", "deferred", "resolved"
    )
    observed, malformed = [], 0
    for group in groups:
        rows = value.get(group, [])
        if not isinstance(rows, list):
            return None  # The exact schema validator reports the malformed group.
        for row in rows:
            refs = row.get("derived_from") if isinstance(row, dict) else None
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                malformed += 1
            if isinstance(refs, list):
                observed.extend(ref for ref in refs if isinstance(ref, str) and ref)
    counts = Counter(observed)
    expected = set(assigned_item_ids)
    missing = [identity for identity in assigned_item_ids if identity not in counts]
    duplicate = [identity for identity in assigned_item_ids if counts[identity] > 1]
    unknown = list(dict.fromkeys(identity for identity in observed if identity not in expected))
    if not missing and not duplicate and not unknown and not malformed:
        return None
    report = {
        "schema_version": PLANNING_COVERAGE_SCHEMA,
        "assigned_item_count": len(assigned_item_ids),
        "assigned_item_ids_sha256": _hash(_json(assigned_item_ids)),
        "observed_reference_count": len(observed),
        "malformed_derived_from_rows": malformed,
    }
    for label, identities in (("missing", missing), ("duplicate", duplicate), ("unknown", unknown)):
        # Host item IDs are short fixed identities. Invalid unbounded model IDs
        # cannot consume the next request's entire budget as diagnostic context.
        shown = [identity for identity in identities if len(identity) <= 128][:MAX_COVERAGE_DIAGNOSTIC_IDS]
        report[f"{label}_item_ids"] = shown
        report[f"omitted_{label}_count"] = len(identities) - len(shown)
    return report


def _planning_coverage_error(diagnostic: dict) -> str:
    fields = [f"{label}={_json(diagnostic[f'{label}_item_ids'])}" for label in ("missing", "duplicate", "unknown")]
    omitted = sum(diagnostic[f"omitted_{label}_count"] for label in ("missing", "duplicate", "unknown"))
    if omitted:
        fields.append(f"additional_unshown_identities={omitted}")
    return "Planning dispositions must cover every exact assigned item ID exactly once; " + "; ".join(fields)


def _reference_coverage(rows: list[dict], expected: list[str], *, retained_structure: tuple[str, ...] = ()) -> None:
    actual = {ref for row in rows for ref in row["source_refs"]}
    if actual | set(retained_structure) != set(expected):
        missing = [ref for ref in expected if ref not in actual and ref not in retained_structure]
        raise ValueError("Every assigned source must remain classified or explicitly deferred; missing="
                         + _json(missing))


def _bounded_output(value: Any, *, _max_output_chars: int = MAX_OUTPUT_CHARS) -> dict:
    if type(_max_output_chars) is not int or not MAX_OUTPUT_CHARS <= _max_output_chars <= 48000:
        raise ValueError("Restored output bound must remain within the complete host packet budget")
    if not isinstance(value, dict) or len(_json(value)) > _max_output_chars:
        raise ValueError(f"Stage output must be an object within {_max_output_chars} characters")
    return value


def validate_index(value: Any, *, input_ids: list[str], source_refs: list[str], routing: bool = False,
                   _retained_structure: tuple[str, ...] = ()) -> dict:
    _bounded_output(value)
    fields = {"schema_version", "input_ids", "methods", "facts", "deferred"}
    schema = INDEX_SCHEMA_V2 if routing else INDEX_SCHEMA
    if routing:
        fields.update({"context_refs", "context_uncertain"})
    if (
        set(value) != fields
        or value["schema_version"] != schema
    ):
        raise ValueError(f"Index must use {schema} with the exact declared fields")
    if routing:
        refs = value["context_refs"]
        if (not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in source_refs for ref in refs)
                or len(refs) != len(set(refs)) or not isinstance(value["context_uncertain"], bool)):
            raise ValueError("Routing must select exact assigned context references and declare uncertainty")
    _coverage(value["input_ids"], input_ids)
    for key, maximum in (("methods", 16), ("facts", 32), ("deferred", 16)):
        rows = value[key]
        if not isinstance(rows, list) or len(rows) > maximum:
            raise ValueError(f"Invalid {key} list")
        for index, row in enumerate(rows):
            keys = (
                {"task", "boundary", "source_refs"}
                if key == "methods"
                else {"text" if key == "facts" else "reason", "source_refs"}
            )
            if not isinstance(row, dict):
                raise ValueError(f"Invalid {key}[{index}]: expected an object with exactly {sorted(keys)}")
            if set(row) != keys:
                missing, unexpected = sorted(keys - set(row)), sorted(set(row) - keys)
                raise ValueError(
                    f"Invalid {key}[{index}] fields: expected exactly {sorted(keys)}; "
                    f"missing={missing}, unexpected={unexpected}. Regenerate this entry with the declared fields."
                )
            for field in keys - {"source_refs"}:
                _text(row[field], 1000, field)
            _refs(row["source_refs"], source_refs)
    if not set(_retained_structure) <= set(source_refs):
        raise ValueError("Retained structure must use exact assigned source references")
    if not any(value[group] for group in ("methods", "facts", "deferred")) and not _retained_structure:
        raise ValueError("Index must contain supported methods or facts, or explicit deferred Evidence")
    _reference_coverage(value["methods"] + value["facts"] + value["deferred"], source_refs,
                        retained_structure=_retained_structure)
    return value


def planning_items(units: list[dict], *, source_files: dict[str, list[str]] | None = None) -> list[dict]:
    """Assign current-row identities; complete ancestry stays in the host's local ledger."""
    items = []
    for unit in units:
        identity = _text(unit.get("id"), 160, "planning unit id")
        for group, kind in (("methods", "method"), ("facts", "fact"), ("deferred", "gap")):
            for ordinal, row in enumerate(unit["value"].get(group, [])):
                item = {
                    key: deepcopy(row[key])
                    for key in ("task", "boundary", "text", "reason", "source_refs") if key in row
                }
                item.update(item_id="item-" + _hash(_json([identity, kind, ordinal, item]))[:24], kind=kind)
                if source_files is not None:
                    item["source_files"] = sorted({
                        uri for ref in item["source_refs"] for uri in source_files.get(ref, [])
                    })
                items.append(item)
    if len({item["item_id"] for item in items}) != len(items):
        raise ValueError("Planning item identities must be unique")
    return items


def _planning_rows(
    value: Any, *, items: list[dict], input_ids: list[str], selection: bool,
    known_candidates: dict[str, dict] | None = None,
    refinement_enabled: bool = False,
    max_input_items: int = 8,
    _max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """Validate item dispositions, deriving source scopes from trusted assigned rows."""
    _bounded_output(value, _max_output_chars=_max_output_chars)
    groups = ("skills", "knowledge", "deferred") if selection else ("methods", "facts", "deferred")
    expected = {"schema_version", "input_ids", "resolved", *groups}
    schema = (SELECTION_SCHEMA_V3 if refinement_enabled else SELECTION_SCHEMA_V2
              if known_candidates is not None else SELECTION_SCHEMA) if selection else MERGE_SCHEMA
    if set(value) != expected or value["schema_version"] != schema:
        raise ValueError(f"Planning output must use the exact {schema} fields")
    try:
        _coverage(value["input_ids"], input_ids)
    except ValueError as exc:
        raise ValueError("Planning input_ids must cover every exact assigned container ID exactly once") from exc
    catalog = {item["item_id"]: item for item in items}
    if len(catalog) != len(items) or any(item.get("kind") not in {"method", "fact", "gap"} for item in items):
        raise ValueError("Planning catalog requires unique typed item identities")
    if selection:
        if type(max_input_items) is not int or not 1 <= max_input_items <= 24:
            raise ValueError("A selection method limit must be an integer from 1 through 24")
        if sum(item["kind"] == "method" for item in items) > max_input_items:
            limit = "eight" if max_input_items == 8 else str(max_input_items)
            raise ValueError(f"A selection window accepts at most {limit} methods; retain others for another window")
    diagnostic = planning_coverage_diagnostics(value, assigned_item_ids=list(catalog))
    if diagnostic is not None and not diagnostic["malformed_derived_from_rows"]:
        raise ValueError(_planning_coverage_error(diagnostic))
    result, consumed = deepcopy(value), []

    def origins(row: dict) -> list[dict]:
        ids = _refs(row.get("derived_from"), list(catalog))
        consumed.extend(ids)
        return [catalog[identity] for identity in ids]

    for group in groups:
        if not isinstance(result[group], list):
            raise ValueError("Planning row groups must be arrays")
        if group == "skills" and len(result[group]) > 8:
            raise ValueError("A selection output supports at most eight candidates")
        for row in result[group]:
            refined = False
            if group == "skills" and isinstance(row, dict) and "revise_candidate_id" in row:
                from .candidate_refinement import BUSINESS_FIELDS, metadata_sha256
                if not refinement_enabled or known_candidates is None:
                    raise ValueError("Business metadata refinement requires the explicit later-round protocol")
                if set(row) != {"derived_from", "revise_candidate_id", "base_metadata_sha256", "reason",
                                *BUSINESS_FIELDS}:
                    raise ValueError("A business refinement requires complete fields and cannot rename identity")
                identity = row["revise_candidate_id"]
                if not isinstance(identity, str) or identity not in known_candidates:
                    raise ValueError("Refinement must name an exact assigned known identity")
                authority = known_candidates[identity]
                if not authority.get("refinement_allowed"):
                    raise ValueError("This known identity is not eligible for source-scoped refinement")
                if (row["base_metadata_sha256"] != authority.get("base_metadata_sha256")
                        or row["base_metadata_sha256"] != metadata_sha256(authority)):
                    raise ValueError("Refinement base metadata hash differs from frozen authority")
                _text(row["reason"], 1000, "refinement reason")
                _refs(row["derived_from"], authority.get("refinement_item_ids", []))
                replacement = {key: deepcopy(row[key]) for key in (*BUSINESS_FIELDS, "derived_from")}
                replacement["name"] = authority["name"]
                row.clear()
                row.update(replacement)
                refined = True
            if (group == "skills" and known_candidates is not None
                    and isinstance(row, dict) and "reuse_candidate_id" in row):
                if set(row) != {"derived_from", "reuse_candidate_id", "reason"}:
                    raise ValueError("Candidate reuse cannot rewrite canonical metadata")
                identity = row["reuse_candidate_id"]
                if not isinstance(identity, str) or identity not in known_candidates:
                    raise ValueError("Candidate reuse must name an exact assigned known identity")
                _text(row["reason"], 1000, "reuse reason")
                authority = known_candidates[identity]
                fields = ("name", "task", "trigger", "inputs", "outputs", "boundary", "rationale")
                canonical = {key: deepcopy(authority[key]) for key in fields}
                canonical["derived_from"] = row["derived_from"]
                row.clear()
                row.update(canonical)
            fields = {"derived_from", "reason"}
            if group == "methods":
                fields = {"derived_from", "task", "boundary"}
            elif group == "facts":
                fields = {"derived_from", "text"}
            elif group == "skills":
                fields = {"derived_from", "name", "task", "trigger", "inputs", "outputs", "boundary", "rationale"}
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("Planning rows must have exact fields; source_refs are host-derived")
            if group == "skills" and known_candidates is not None and not refined:
                authority = next((candidate for candidate in known_candidates.values()
                                  if candidate["name"] == row["name"]), None)
                if authority and any(row[key] != authority[key] for key in (
                    "task", "trigger", "inputs", "outputs", "boundary",
                )):
                    raise ValueError("Known candidate name cannot rewrite its canonical workflow; use explicit reuse")
            parents = origins(row)
            kinds = {parent["kind"] for parent in parents}
            if not selection:
                required_kind = {"methods": "method", "facts": "fact", "deferred": "gap"}[group]
                if kinds != {required_kind}:
                    raise ValueError("Intermediate merging must preserve method, fact and provisional-gap kinds")
            elif group == "skills" and "method" not in kinds:
                raise ValueError("Facts alone cannot be forced into a Skill Candidate")
            elif group in {"skills", "knowledge"} and "gap" in kinds:
                raise ValueError("Unresolved gaps need an explicit deferred or supported resolution")
            elif group == "deferred" and kinds == {"fact"}:
                raise ValueError("Supported facts alone must remain knowledge, not an Evidence gap")
            elif group in {"knowledge", "deferred"} and sum(parent["kind"] == "method" for parent in parents) > 1:
                raise ValueError("Each unselected method needs its own explicit disposition reason")
            for field in fields - {"derived_from", "inputs", "outputs"}:
                _text(row[field], 64 if field == "name" else 400 if selection else 1000, field)
            row["source_refs"] = sorted({ref for parent in parents for ref in parent["source_refs"]})

    if not isinstance(result["resolved"], list):
        raise ValueError("Resolved gaps must be an array")
    for row in result["resolved"]:
        if not isinstance(row, dict) or set(row) != {"derived_from", "supported_by", "reason"}:
            raise ValueError("Resolved gap requires its identities, support and explanation")
        parents = origins(row)
        supports = [catalog[identity] for identity in _refs(row["supported_by"], list(catalog))]
        _text(row["reason"], 1000, "resolution reason")
        if any(parent["kind"] != "gap" for parent in parents) or any(item["kind"] == "gap" for item in supports):
            raise ValueError("Only provisional gaps may be resolved by method or fact support")
        for parent in parents:
            original_files = set(parent.get("source_files") or [])
            supported_files = {uri for item in supports for uri in item.get("source_files") or []}
            if not original_files or not original_files <= supported_files:
                raise ValueError(
                    "Gap resolution requires trusted support from the same original source file for every affected file"
                )
        row["source_refs"] = sorted({ref for item in parents + supports for ref in item["source_refs"]})
        for group in groups:
            for supported_row in result[group]:
                if set(supported_row["derived_from"]) & set(row["supported_by"]):
                    supported_row["source_refs"] = sorted(set(supported_row["source_refs"]) | set(row["source_refs"]))
    try:
        _coverage(consumed, list(catalog))
    except ValueError as exc:
        diagnostic = planning_coverage_diagnostics(value, assigned_item_ids=list(catalog))
        raise ValueError(_planning_coverage_error(diagnostic)) from exc
    if selection:
        validate_plan(
            selection_plan(result),
            source_refs=sorted({ref for item in items for ref in item["source_refs"]}),
            bounded=False,
            require_nonempty_deferred=False,
            require_reference_coverage=False,
        )
    return result


def validate_merge(value: Any, *, items: list[dict], input_ids: list[str]) -> dict:
    return _planning_rows(value, items=items, input_ids=input_ids, selection=False)


def validate_selection(
    value: Any, *, items: list[dict], input_ids: list[str], known_candidates: dict[str, dict] | None = None,
    refinement_enabled: bool = False,
    max_input_items: int = 8,
    _max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    return _planning_rows(value, items=items, input_ids=input_ids, selection=True, known_candidates=known_candidates,
                          refinement_enabled=refinement_enabled, max_input_items=max_input_items,
                          _max_output_chars=_max_output_chars)


def selection_plan(value: dict) -> dict:
    """Render the existing public plan, keeping richer per-item decisions in the local audit."""
    return {
        "schema_version": PLAN_SCHEMA,
        "skills": [
            {key: deepcopy(item) for key, item in row.items() if key != "derived_from"}
            for row in value["skills"]
        ],
        "deferred": [
            {"reason": row["reason"], "source_refs": list(row["source_refs"])}
            for row in value["deferred"]
        ],
    }


def validate_plan(
    value: Any,
    *,
    source_refs: list[str],
    input_ids: list[str] | None = None,
    bounded: bool = True,
    require_nonempty_deferred: bool = True,
    require_reference_coverage: bool = True,
) -> dict:
    if bounded:
        _bounded_output(value)
    elif not isinstance(value, dict):
        raise ValueError("Plan must be an object")
    required = {"schema_version", "skills", "deferred"} | ({"input_ids"} if input_ids is not None else set())
    if set(value) != required or value["schema_version"] != PLAN_SCHEMA:
        raise ValueError("Plan must use skill-miner.plan.v1 with exact fields")
    if input_ids is not None:
        _coverage(value["input_ids"], input_ids)
    if (
        not isinstance(value["skills"], list)
        or (bounded and len(value["skills"]) > 8)
        or not isinstance(value["deferred"], list)
    ):
        raise ValueError("A bounded plan supports zero to eight candidates")
    names = set()
    for skill in value["skills"]:
        if not isinstance(skill, dict) or set(skill) != {
            "name",
            "task",
            "trigger",
            "inputs",
            "outputs",
            "boundary",
            "rationale",
            "source_refs",
        }:
            raise ValueError("Candidate plan fields are incomplete")
        name = _text(skill["name"], 64, "name")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name in names:
            raise ValueError("Candidate names must be unique safe lowercase slugs")
        names.add(name)
        for field in ("task", "trigger", "boundary", "rationale"):
            _text(skill[field], 400, field)
        for field in ("inputs", "outputs"):
            if not isinstance(skill[field], list) or not skill[field] or len(skill[field]) > 16:
                raise ValueError("Candidate inputs/outputs must be nonempty arrays")
            for item in skill[field]:
                _text(item, 240, field)
        _refs(skill["source_refs"], source_refs)
    if bounded and len(value["deferred"]) > 32:
        raise ValueError("Compact plan deferred list is too long")
    for row in value["deferred"]:
        if not isinstance(row, dict) or set(row) != {"reason", "source_refs"}:
            raise ValueError("Deferred plan entry requires reason and source_refs")
        _text(row["reason"], 400, "reason")
        _refs(row["source_refs"], source_refs)
    if require_nonempty_deferred and not value["skills"] and not value["deferred"]:
        raise ValueError("Zero candidates must explain why Evidence is deferred")
    if require_reference_coverage:
        _reference_coverage(value["skills"] + value["deferred"], source_refs)
    return value


def validate_deferred(value: Any, *, source_refs: list[str]) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "reason", "source_refs"}
        or value["schema_version"] != DEFERRED_SCHEMA
    ):
        raise ValueError("Deferred output requires schema_version, reason and exact source_refs")
    _text(value["reason"], 2000, "reason")
    _refs(value["source_refs"], source_refs)
    return value


def is_deferred(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema_version") == DEFERRED_SCHEMA


def validate_section(value: Any, *, input_ids: list[str]) -> dict:
    _bounded_output(value)
    if set(value) != {"schema_version", "input_ids", "title", "content"} or value["schema_version"] != SECTION_SCHEMA:
        raise ValueError("Skill section must use the exact skill-miner.section.v1 fields")
    _coverage(value["input_ids"], input_ids)
    _text(value["title"], 200, "title")
    _text(value["content"], 10000, "content")
    if value["content"].lstrip().startswith("---"):
        raise ValueError("Skill section cannot replace the program-owned frontmatter")
    return value


def validate_section_skip(value: Any, *, input_ids: list[str]) -> dict:
    """Record inspected ranges that add no rules within this candidate's task boundary."""
    _bounded_output(value)
    if set(value) != {"schema_version", "input_ids", "reason"} or value["schema_version"] != SECTION_SKIP_SCHEMA:
        raise ValueError("Skipped section must use the exact skill-miner.section-skip.v1 fields")
    _coverage(value["input_ids"], input_ids)
    _text(value["reason"], 2000, "reason")
    return value


class NoOperationalSkillSectionsError(ValueError):
    """All inspected source ranges were skipped within the candidate's task boundary."""


def iter_skill_chunks(
    candidate: dict, sections: Iterable[dict], *, expected_input_ids: list[str]
) -> Iterator[str]:
    """Validate one section at a time and render exactly the canonical Skill bytes."""
    if not expected_input_ids:
        raise ValueError("A Skill needs source-backed sections with complete assigned Evidence coverage")
    expected, covered = set(expected_input_ids), set()
    # Rendering a tiny placeholder reuses the shared canonical frontmatter and
    # its validation, without assembling a potentially large complete body.
    yield render_skill_md(name=candidate["name"], description=candidate["task"], content="#")[:-2]
    yield f"# {candidate['task']}\n\n{candidate['boundary']}\n"
    pending, normal_count = "", 0
    for section in sections:
        if section.get("schema_version") == SECTION_SKIP_SCHEMA:
            validate_section_skip(section, input_ids=section.get("input_ids", []))
        else:
            validate_section(section, input_ids=section.get("input_ids", []))
            if pending:
                yield pending
            normal_count += 1
            pending = f"\n## {normal_count}. {section['title']}\n\n{section['content']}\n"
        identities = set(section["input_ids"])
        if covered.intersection(identities):
            raise ValueError("Stage input_ids must cover every exact assigned input ID")
        covered.update(identities)
    if covered != expected:
        raise ValueError("Stage input_ids must cover every exact assigned input ID")
    if not normal_count:
        raise NoOperationalSkillSectionsError("A Skill needs at least one source-backed operational section")
    # Only the final normal section loses trailing whitespace, matching the
    # shared renderer's body.strip() followed by its single terminal newline.
    yield pending.rstrip() + "\n"


def compose_skill(candidate: dict, sections: Iterable[dict], *, expected_input_ids: list[str]) -> str:
    """Compatibility wrapper for callers that explicitly need the complete text."""
    return "".join(iter_skill_chunks(candidate, sections, expected_input_ids=expected_input_ids))


def expand_plan_work(plan: dict) -> list[dict]:
    """Expand fixed work in TE; neither model output nor Evidence text is repeated."""
    refs = sorted({ref for row in plan["skills"] + plan["deferred"] for ref in row["source_refs"]})
    tasks = [
        {
            "key": f"shared-{role}",
            "stage": role,
            "output_path": f"analysis/{path}",
            "source_refs": refs,
        }
        for role, path in (
            ("evidence", "evidence-map.md"),
            ("gaps", "gaps.md"),
            ("review", "review.json"),
            ("plan", "capability-plan.json"),
        )
    ]
    for candidate in plan["skills"]:
        name = candidate["name"]
        for role, path in (
            ("skill_section", "SKILL.md"),
            ("evaluation", "EVALUATION.md"),
            ("benchmark_view", "BENCHMARK.md"),
        ):
            tasks.append(
                {
                    "key": f"{name}-{role}",
                    "stage": role,
                    "skill_name": name,
                    "output_path": f"compiled_skill/{name}/{path}",
                    "source_refs": candidate["source_refs"],
                }
            )
        for index in range(1, 13):
            case_id = f"{name}-case-{index:02d}"
            tasks.append(
                {
                    "key": case_id,
                    "stage": "case",
                    "skill_name": name,
                    "case_id": case_id,
                    "output_path": f"case-parts/{name}/{case_id}.json",
                    "source_refs": candidate["source_refs"],
                }
            )
    return tasks


def validate_case(value: Any, *, case_id: str, skill_name: str, source_refs: list[str]) -> dict:
    _bounded_output(value)
    if set(value) != CASE_KEYS or value.get("case_id") != case_id or value.get("skill_ids") != [skill_name]:
        raise ValueError("Case requires exact canonical fields and assigned identities")
    _text(value["query"], MAX_OUTPUT_CHARS, "query")
    if not isinstance(value["materials"], list) or not isinstance(value["provenance"], dict):
        raise ValueError("Case materials/provenance shape is invalid")
    _refs(value["provenance"].get("session_ids"), source_refs)
    if not isinstance(value["replay"], dict) or value["replay"].get("progressive_disclosure") != DISCLOSURE:
        raise ValueError("Case progressive_disclosure must retain canonical settings")
    if (
        not isinstance(value["metadata"], dict)
        or value["metadata"].get("split") != "test"
        or value["metadata"].get("minimum_requirement_target") != 12
    ):
        raise ValueError("Case metadata must retain the canonical test settings")
    document = assemble_dataset(
        [value], skill_name=skill_name, task="Case validation", identity=case_id, validate=False
    )
    errors = benchmark_format.validate_document(document, expected_skill_name=skill_name)
    if errors:
        raise ValueError("; ".join(errors))
    checks, ids, texts = value["checks"], set(), {"output": set(), "trajectory": set()}
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "kind", "text", "polarity"}:
            raise ValueError("Check must contain only canonical fields")
        check_id = _text(check.get("id"), 160, "check id")
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in texts or check_id in ids:
            raise ValueError("Check kind/ID is invalid")
        text = _text(check.get("text"), 4000, "check text").strip()
        if text in texts[kind] or ("polarity" in check and check["polarity"] != "must_not"):
            raise ValueError("Duplicate Checklist or invalid polarity")
        ids.add(check_id)
        texts[kind].add(text)
    if not 12 <= len(texts["output"]) <= 24 or not 1 <= len(texts["trajectory"]) <= 8:
        raise ValueError("Case must contain 12-24 distinct output checks and 1-8 trajectory checks")
    return value


def normalize_case_materials(value: Any) -> tuple[Any, bool]:
    """Losslessly normalize unambiguous materials shape drift.

    The public dataset contract requires ``materials`` to be an array. Some
    model outputs encode several named materials as an object, omit the
    enclosing array around one material record, or supply a plain text block.
    These forms need no new business content or remote Compile to repair.
    """
    if not isinstance(value, dict):
        return value, False
    materials = value.get("materials")
    if isinstance(materials, list):
        return value, False
    if isinstance(materials, str) and materials.strip():
        normalized = deepcopy(value)
        normalized["materials"] = [{"content": materials}]
        return normalized, True
    if not isinstance(materials, dict):
        return value, False

    normalized = deepcopy(value)
    fields = {"path", "name", "content", "content_b64", "mime_type"}
    if materials and set(materials) <= fields and any(key in materials for key in ("path", "content", "content_b64")):
        # A single material record was emitted without its array wrapper.
        normalized["materials"] = [materials]
    else:
        # Named source-derived items (e.g. scenario/source_dialogue) remain
        # separate and retain their exact keys and values as content.
        normalized["materials"] = [
            {"name": key, "content": item} for key, item in materials.items()
        ]
    return normalized, True


def assemble_dataset(
    cases: list[dict],
    *,
    skill_name: str,
    task: str,
    identity: str,
    validate: bool = True,
    expected_case_ids: list[str] | None = None,
) -> dict:
    expected = [f"{skill_name}-case-{index:02d}" for index in range(1, 13)]
    if expected_case_ids is not None:
        if (
            not isinstance(expected_case_ids, list) or len(expected_case_ids) != 12
            or any(
                not isinstance(value, str) or not value or len(value) > 160 or not value[0].isalnum()
                or any(not (character.isalnum() or character in "_.:-") for character in value)
                for value in expected_case_ids
            )
            or len(set(expected_case_ids)) != 12
        ):
            raise ValueError("Expected case identities must be twelve unique safe nonempty strings")
        expected = expected_case_ids
    if validate:
        if [case.get("case_id") for case in cases] != expected:
            raise ValueError("Dataset needs twelve complete independently validated cases in stable order")
        refs = sorted({ref for case in cases for ref in case.get("provenance", {}).get("session_ids", [])})
        queries = {}
        for case in cases:
            validate_case(case, case_id=case["case_id"], skill_name=skill_name, source_refs=refs)
            normalized = normalize_case_query(case["query"])
            if normalized in queries:
                raise ValueError(
                    "Dataset cases must have distinct normalized queries; "
                    f"{case['case_id']} repeats {queries[normalized]}"
                )
            queries[normalized] = case["case_id"]
    now = datetime.now(timezone.utc).isoformat()
    document = {
        "schema_version": "team-replay.dataset",
        "dataset_id": f"skill-miner-{skill_name}-{_hash(identity)[:16]}",
        "name": task,
        "description": task,
        "skills": [{"skill_id": skill_name}],
        "source": {"kind": "skill_miner"},
        "cases": cases,
        "created_at": now,
        "updated_at": now,
        "metadata": {"candidate_revision": 1},
    }
    if validate:
        errors = benchmark_format.validate_document(document, expected_skill_name=skill_name)
        if errors:
            raise ValueError("; ".join(errors))
    return document
