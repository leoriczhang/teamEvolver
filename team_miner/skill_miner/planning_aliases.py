"""Frozen packet-local planning identities; business text and Evidence remain unchanged.

This pure helper does not submit work or repair guesses. The host persists both
packets and the manifest before submission and applies its existing validators
after decoding the exact authorized machine fields.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Any

PROTOCOL = "te-planning-aliases.v1"
SELECTION_SCHEMAS = {f"skill-miner.selection.v{number}" for number in (1, 2, 3)}
CAPACITY_SCHEMA = "skill-miner.selection.capacity.v1"
MAX_OUTPUT_CHARS = 12000
_MANIFEST_FIELDS = {
    "schema_version", "selection_schema", "methods", "candidates", "references",
    "container_ids", "source_refs", "readonly_refs", "canonical_packet_sha256",
    "aliased_packet_sha256", "manifest_sha256",
}
_BUSINESS_FIELDS = {"task", "trigger", "inputs", "outputs", "boundary", "rationale"}


class PlanningAliasManifestError(ValueError):
    """Frozen host metadata is inconsistent; another model call cannot repair it."""


class PlanningAliasOutputError(ValueError):
    """Model bookkeeping is invalid; no partial dispositions are accepted."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _ids(value: Any, *, empty: bool = False, output: bool = False) -> list[str]:
    error = PlanningAliasOutputError if output else PlanningAliasManifestError
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(item, str) or not item or item.strip() != item for item in value)
            or len(set(value)) != len(value)):
        raise error("Planning identities must be exact unique nonempty strings")
    return value


def _select_ids(value: Any, reverse: dict[str, str], *, empty: bool = False) -> list[str]:
    ids = _ids(value, empty=empty)
    if any(identity not in reverse for identity in ids):
        raise PlanningAliasManifestError("A machine field names an identity outside its frozen role")
    return [reverse[identity] for identity in ids]


def _readonly_rows(controls: dict):
    for field in ("related_facts", "provisional_gaps"):
        rows = controls.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PlanningAliasManifestError("Readonly planning notes must be objects")
        yield from rows


def _build(packet: Any) -> tuple[dict, dict]:
    if (not isinstance(packet, dict) or packet.get("stage") != "plan"
            or not isinstance(packet.get("derived_controls"), dict)):
        raise PlanningAliasManifestError("Aliases are only supported for a frozen typed selection packet")
    original = packet["derived_controls"]
    schema = original.get("selection_schema", "skill-miner.selection.v1")
    if schema not in SELECTION_SCHEMAS or original.get("planning_alias_protocol") is not None:
        raise PlanningAliasManifestError("Selection schema must be supported and not already aliased")
    items = original.get("items")
    if (not isinstance(items, list) or not 1 <= len(items) <= 24
            or any(not isinstance(row, dict) or row.get("kind") != "method" for row in items)):
        raise PlanningAliasManifestError("Planning aliases require one through 24 assigned methods")
    method_ids = _ids([row.get("item_id") for row in items])
    known = original.get("known_candidates", [])
    if (not isinstance(known, list) or len(known) > 8
            or any(not isinstance(row, dict) for row in known)):
        raise PlanningAliasManifestError("Known candidate authority must be a bounded list")
    candidate_ids = _ids([row.get("candidate_id") for row in known], empty=True)
    if schema == "skill-miner.selection.v1" and candidate_ids:
        raise PlanningAliasManifestError("Candidate reuse requires the explicit selection protocol")
    containers = _ids(original.get("input_ids"))
    sources = _ids(original.get("allowed_source_refs"))
    readonly = []
    for row in [*items, *_readonly_rows(original)]:
        for field in ("source_file_ids", "source_fragment_ids"):
            for identity in _ids(row.get(field, []), empty=True):
                if identity not in readonly:
                    readonly.append(identity)
    reference_ids = list(dict.fromkeys([*containers, *sources, *readonly]))
    if len(reference_ids) > 999:
        raise PlanningAliasManifestError("The bounded planning reference catalog exceeds 999 identities")
    all_ids = method_ids + candidate_ids + reference_ids
    if (len(set(all_ids)) != len(all_ids)
            or any(re.fullmatch(r"[mcu]\d{3}", identity) for identity in all_ids)):
        raise PlanningAliasManifestError("Canonical planning roles must not collide with each other or short aliases")
    methods = {f"m{index:03d}": identity for index, identity in enumerate(method_ids, 1)}
    candidates = {f"c{index:03d}": identity for index, identity in enumerate(candidate_ids, 1)}
    references = {f"u{index:03d}": identity for index, identity in enumerate(reference_ids, 1)}
    mr, cr, ur = ({value: key for key, value in mapping.items()} for mapping in (methods, candidates, references))
    result = copy.deepcopy(packet)
    controls = result["derived_controls"]
    controls["planning_alias_protocol"] = PROTOCOL
    controls["input_ids"] = _select_ids(containers, ur)
    controls["allowed_source_refs"] = _select_ids(sources, ur)
    if "input_ids" in result:
        result["input_ids"] = _select_ids(result["input_ids"], {identity: ur[identity] for identity in containers},
                                           empty=True)
    covered_sources = set()
    for row in controls["items"]:
        row["item_id"] = mr[row["item_id"]]
        row_sources = _ids(row.get("source_refs"))
        covered_sources.update(row_sources)
        row["source_refs"] = _select_ids(row_sources, {identity: ur[identity] for identity in sources})
    if covered_sources != set(sources):
        raise PlanningAliasManifestError("Every frozen allowed source scope must belong to an assigned method")
    for row in controls.get("known_candidates", []):
        row["candidate_id"] = cr[row["candidate_id"]]
        if "refinement_item_ids" in row:
            row["refinement_item_ids"] = _select_ids(row["refinement_item_ids"], mr, empty=True)
    for row in [*controls["items"], *_readonly_rows(controls)]:
        for field in ("source_file_ids", "source_fragment_ids"):
            if field in row:
                row[field] = _select_ids(row[field], ur, empty=True)
    manifest = {
        "schema_version": PROTOCOL, "selection_schema": schema,
        "methods": methods, "candidates": candidates, "references": references,
        "container_ids": [ur[identity] for identity in containers],
        "source_refs": [ur[identity] for identity in sources],
        "readonly_refs": [ur[identity] for identity in readonly],
        "canonical_packet_sha256": _digest(packet), "aliased_packet_sha256": _digest(result),
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return result, manifest


def build_planning_alias_packet(packet: Any) -> tuple[dict, dict]:
    """Freeze a deterministic per-packet mapping without changing the input object."""
    try:
        result, manifest = _build(packet)
        validate_planning_alias_manifest(manifest, packet=result)
        return result, manifest
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        if isinstance(exc, PlanningAliasManifestError):
            raise
        raise PlanningAliasManifestError("Canonical planning packet cannot be aliased") from exc


def validate_planning_alias_manifest(
    manifest: Any, *, packet: dict | None = None, canonical_packet: dict | None = None,
) -> None:
    """Validate role bijections/digests; optionally bind both frozen packet copies.

    The host additionally verifies the original persisted sidecar SHA. A digest
    stored inside an untrusted replacement manifest alone is not authentication.
    """
    if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS
            or manifest["schema_version"] != PROTOCOL or not isinstance(manifest["selection_schema"], str)
            or manifest["selection_schema"] not in SELECTION_SCHEMAS):
        raise PlanningAliasManifestError("Invalid frozen planning alias manifest")
    try:
        for field in ("canonical_packet_sha256", "aliased_packet_sha256", "manifest_sha256"):
            if not isinstance(manifest[field], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[field]):
                raise PlanningAliasManifestError("Planning manifest requires exact SHA-256 digests")
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        if _digest(unsigned) != manifest["manifest_sha256"]:
            raise PlanningAliasManifestError("Frozen planning alias manifest SHA differs")
        canonical_ids = []
        for field, prefix, minimum, maximum in (("methods", "m", 1, 24), ("candidates", "c", 0, 8),
                                                ("references", "u", 1, 999)):
            mapping = manifest[field]
            if (not isinstance(mapping, dict) or not minimum <= len(mapping) <= maximum
                    or list(mapping) != [f"{prefix}{index:03d}" for index in range(1, len(mapping) + 1)]):
                raise PlanningAliasManifestError("Planning aliases must be contiguous within their exact role")
            ids = _ids(list(mapping.values()), empty=minimum == 0)
            if any(re.fullmatch(r"[mcu]\d{3}", identity) for identity in ids):
                raise PlanningAliasManifestError("A canonical identity cannot already be a short alias")
            canonical_ids.extend(ids)
        if len(canonical_ids) != len(set(canonical_ids)):
            raise PlanningAliasManifestError("Frozen planning mappings are not independent bijections")
        used = set()
        for field in ("container_ids", "source_refs", "readonly_refs"):
            ids = _ids(manifest[field], empty=field == "readonly_refs")
            if any(identity not in manifest["references"] for identity in ids):
                raise PlanningAliasManifestError("Reference roles must use exact assigned u aliases")
            used.update(ids)
        if used != set(manifest["references"]):
            raise PlanningAliasManifestError("Every reference alias must have a declared frozen role")
        if manifest["selection_schema"] == "skill-miner.selection.v1" and manifest["candidates"]:
            raise PlanningAliasManifestError("Legacy selection cannot authorize candidate reuse")
        if packet is not None:
            if _digest(packet) != manifest["aliased_packet_sha256"]:
                raise PlanningAliasManifestError("Frozen aliased packet SHA differs")
            controls = packet["derived_controls"]
            if (packet.get("stage") != "plan" or controls.get("planning_alias_protocol") != PROTOCOL
                    or controls.get("selection_schema", "skill-miner.selection.v1") != manifest["selection_schema"]
                    or [row["item_id"] for row in controls["items"]] != list(manifest["methods"])
                    or [row["candidate_id"] for row in controls.get("known_candidates", [])]
                    != list(manifest["candidates"])
                    or controls["input_ids"] != manifest["container_ids"]
                    or controls["allowed_source_refs"] != manifest["source_refs"]):
                raise PlanningAliasManifestError("Frozen alias packet has different assigned roles")
            source_ids = set()
            for row in controls["items"]:
                ids = _ids(row.get("source_refs"))
                if row.get("kind") != "method" or any(identity not in manifest["source_refs"] for identity in ids):
                    raise PlanningAliasManifestError("Assigned methods must retain their exact allowed source roles")
                source_ids.update(ids)
            if source_ids != set(manifest["source_refs"]):
                raise PlanningAliasManifestError("Frozen alias packet omitted an assigned source scope")
            readonly_ids = set()
            for row in [*controls["items"], *_readonly_rows(controls)]:
                for field in ("source_file_ids", "source_fragment_ids"):
                    ids = _ids(row.get(field, []), empty=True)
                    if any(identity not in manifest["readonly_refs"] for identity in ids):
                        raise PlanningAliasManifestError("Readonly aliases cannot change their frozen role")
                    readonly_ids.update(ids)
            if readonly_ids != set(manifest["readonly_refs"]):
                raise PlanningAliasManifestError("Frozen readonly alias assignments differ")
            for authority in controls.get("known_candidates", []):
                ids = _ids(authority.get("refinement_item_ids", []), empty=True)
                if any(identity not in manifest["methods"] for identity in ids):
                    raise PlanningAliasManifestError("Refinement permissions must use exact assigned m aliases")
        if canonical_packet is not None:
            expected_packet, expected_manifest = _build(canonical_packet)
            if expected_manifest != manifest or (packet is not None and expected_packet != packet):
                raise PlanningAliasManifestError("Frozen alias mapping differs from the original canonical packet")
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        if isinstance(exc, PlanningAliasManifestError):
            raise
        raise PlanningAliasManifestError("Malformed frozen planning alias metadata") from exc


def _output_ids(value: Any, mapping: dict, *, expected: list[str] | None = None) -> list[str]:
    ids = _ids(value, output=True)
    unknown = [identity for identity in ids if identity not in mapping]
    if unknown:
        shown = [identity for identity in unknown if len(identity) <= 32][:8]
        raise PlanningAliasOutputError(f"Output uses unknown or wrong-role aliases: {_json(shown)}")
    if expected is not None and set(ids) != set(expected):
        raise PlanningAliasOutputError("Output must use complete exact assigned aliases in the declared role")
    return [mapping[identity] for identity in ids]


def decode_planning_alias_output(raw: Any, manifest: dict, *, max_capacity_reason_chars: int = 1000,
                                max_skill_rows: int = 8) -> dict:
    """Reject incomplete/unknown bookkeeping before restoring exact canonical identities."""
    validate_planning_alias_manifest(manifest)
    if type(max_capacity_reason_chars) is not int or not 1 <= max_capacity_reason_chars <= MAX_OUTPUT_CHARS:
        raise ValueError("Capacity diagnostic bound must remain within the raw output budget")
    if type(max_skill_rows) is not int or not 1 <= max_skill_rows <= 24:
        raise ValueError("Selection row bound must remain within the assigned-method limit")
    try:
        if not isinstance(raw, dict) or len(_json(raw)) > MAX_OUTPUT_CHARS:
            raise PlanningAliasOutputError("Planning output must be an object within 12000 characters")
        result = copy.deepcopy(raw)
        methods, candidates, references = (manifest[field] for field in ("methods", "candidates", "references"))
        header = _ids(result.get("input_ids"), output=True)
        if result.get("schema_version") == CAPACITY_SCHEMA:
            if set(result) != {"schema_version", "input_ids", "source_refs", "reason"}:
                raise PlanningAliasOutputError("Capacity output must contain exactly its declared fields")
            result["input_ids"] = _output_ids(header, references, expected=manifest["container_ids"])
            result["source_refs"] = _output_ids(result["source_refs"], references, expected=manifest["source_refs"])
            if (not isinstance(result["reason"], str) or not result["reason"].strip()
                    or len(result["reason"]) > max_capacity_reason_chars):
                raise PlanningAliasOutputError("Capacity reason must remain nonempty within its diagnostic bound")
            return result
        if (set(result) != {"schema_version", "input_ids", "skills", "knowledge", "deferred", "resolved"}
                or result["schema_version"] != manifest["selection_schema"]
                or set(header) not in (set(manifest["container_ids"]), set(methods))):
            raise PlanningAliasOutputError("Selection requires its exact schema and a complete assignment header")
        result["input_ids"] = [references[identity] for identity in manifest["container_ids"]]
        if result["resolved"] != []:
            raise PlanningAliasOutputError("Method-only selection cannot consume readonly gaps or facts")
        consumed = []
        for group in ("skills", "knowledge", "deferred"):
            rows = result[group]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise PlanningAliasOutputError("Selection disposition groups must be arrays of objects")
            if group == "skills" and len(rows) > max_skill_rows:
                raise PlanningAliasOutputError(f"A bounded selection has at most {max_skill_rows} Skill rows")
            for row_index, row in enumerate(rows):
                fields = {"derived_from", "reason"}
                if group == "skills":
                    if "revise_candidate_id" in row:
                        if manifest["selection_schema"] != "skill-miner.selection.v3":
                            raise PlanningAliasOutputError("Refinement requires its explicit selection protocol")
                        fields = {"derived_from", "revise_candidate_id", "base_metadata_sha256", "reason",
                                  *_BUSINESS_FIELDS}
                    elif "reuse_candidate_id" in row:
                        if manifest["selection_schema"] == "skill-miner.selection.v1":
                            raise PlanningAliasOutputError("Candidate reuse requires its explicit selection protocol")
                        fields = {"derived_from", "reuse_candidate_id", "reason"}
                        # Existing V2 handling removes only byte-equivalent redundant
                        # authority fields. Preserve them here for that unchanged
                        # validator; never remove or rewrite model business text.
                        if (manifest["selection_schema"] == "skill-miner.selection.v2"
                                and set(row) - fields <= {"name", *_BUSINESS_FIELDS}):
                            fields = set(row)
                    else:
                        fields = {"derived_from", "name", *_BUSINESS_FIELDS}
                if set(row) != fields:
                    raise PlanningAliasOutputError(
                        "Selection rows must have exactly the declared machine/business fields"
                    )
                try:
                    ids = _ids(row["derived_from"], output=True)
                    row["derived_from"] = _output_ids(ids, methods)
                except PlanningAliasOutputError as exc:
                    raise PlanningAliasOutputError(
                        f"{group}[{row_index}].derived_from: {exc}; expected a nonempty array of unique "
                        'exact assigned m alias strings, e.g. ["m001","m002"], not objects or a single string'
                    ) from exc
                consumed.extend(ids)
                for field in ("reuse_candidate_id", "revise_candidate_id"):
                    if field in row:
                        if not isinstance(row[field], str) or row[field] not in candidates:
                            raise PlanningAliasOutputError("Reuse/refinement must name an exact assigned c alias")
                        row[field] = candidates[row[field]]
        counts = Counter(consumed)
        if set(counts) != set(methods) or any(count != 1 for count in counts.values()):
            missing = [identity for identity in methods if identity not in counts]
            duplicate = [identity for identity in methods if counts[identity] > 1]
            raise PlanningAliasOutputError(
                "Every assigned m alias must be disposed exactly once; "
                f"missing={_json(missing)}; duplicate={_json(duplicate)}; no partial output accepted"
            )
        return result
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        if isinstance(exc, PlanningAliasOutputError):
            raise
        raise PlanningAliasOutputError("Malformed aliased planning output") from exc
