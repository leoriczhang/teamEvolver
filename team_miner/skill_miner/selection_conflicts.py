"""Bounded repair hints for exact existing identities; never business-field reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from . import bounded_compile_plan as planning
from .candidate_identity import IDENTITY_FIELDS, IdentityCatalog, candidate_id
from .candidate_refinement import metadata_sha256

MAX_CONFLICTS = 24  # Eight rows per output, three finite attempts.
WORKFLOW_FIELDS = ("task", "trigger", "inputs", "outputs", "boundary")


def _coverage_hint(diagnostic, controls: dict) -> dict | None:
    """Require repair notes to name the exact frozen assignment, never another window."""
    if diagnostic is None:
        return None
    assigned = [row["item_id"] for row in controls["items"]]
    expected_hash = hashlib.sha256(json.dumps(assigned, ensure_ascii=False, separators=(",", ":"),
                                              allow_nan=False).encode()).hexdigest()
    fields = {"schema_version", "assigned_item_count", "assigned_item_ids_sha256", "observed_reference_count",
              "malformed_derived_from_rows"}
    fields.update(f"{label}_item_ids" for label in ("missing", "duplicate", "unknown"))
    fields.update(f"omitted_{label}_count" for label in ("missing", "duplicate", "unknown"))
    if (not isinstance(diagnostic, dict) or set(diagnostic) != fields
            or diagnostic["schema_version"] != planning.PLANNING_COVERAGE_SCHEMA
            or diagnostic["assigned_item_ids_sha256"] != expected_hash
            or diagnostic["assigned_item_count"] != len(assigned)):
        raise ValueError("Planning coverage repair notes must match the exact frozen item assignment")
    for key in ("assigned_item_count", "observed_reference_count", "malformed_derived_from_rows",
                "omitted_missing_count", "omitted_duplicate_count", "omitted_unknown_count"):
        if type(diagnostic[key]) is not int or diagnostic[key] < 0:
            raise ValueError("Planning coverage repair counts must be nonnegative integers")
    for label in ("missing", "duplicate", "unknown"):
        identities = diagnostic[f"{label}_item_ids"]
        if (not isinstance(identities, list) or len(identities) > planning.MAX_COVERAGE_DIAGNOSTIC_IDS
                or any(not isinstance(identity, str) or not identity or len(identity) > 128 for identity in identities)
                or len(identities) != len(set(identities))
                or (label != "unknown" and not set(identities) <= set(assigned))
                or (label == "unknown" and set(identities) & set(assigned))):
            raise ValueError("Planning coverage repair identities must be exact bounded assignment diagnostics")
    return copy.deepcopy(diagnostic)


def _authority(job: dict, root: Path, name: str, controls: dict) -> dict | None:
    path = root / "selection-identities" / "catalog.sqlite3"
    if path.is_file():
        catalog = IdentityCatalog(path)
        try:
            row = catalog.named(name)
            if row:
                result = {"candidate_id": row["candidate_id"], **{
                    key: copy.deepcopy(row["candidate"][key]) for key in IDENTITY_FIELDS
                }, "candidate_metadata_is_evidence": False}
                if controls.get("selection_schema") == planning.SELECTION_SCHEMA_V3:
                    eligible = []
                    for item in controls["items"]:
                        # Eligibility is copied only from the frozen authority;
                        # a naming collision cannot grant a business revision.
                        original = next((old for old in controls.get("known_candidates", [])
                                         if old["candidate_id"] == row["candidate_id"]), None)
                        if original and item["item_id"] in original.get("refinement_item_ids", []):
                            eligible.append(item["item_id"])
                    result.update(base_metadata_sha256=metadata_sha256(result),
                                  refinement_allowed=bool(eligible), refinement_item_ids=eligible)
                return result
        finally:
            catalog.close()
    # This supports hosts without a retrieval catalog, without fuzzy matching.
    path = root / "plan.json"
    if path.is_file():
        plan = json.loads(path.read_text(encoding="utf-8"))
        for row in plan["skills"]:
            if row["name"] == name:
                return {"candidate_id": candidate_id(job["job_id"], name), **{
                    key: copy.deepcopy(row[key]) for key in IDENTITY_FIELDS
                }, "candidate_metadata_is_evidence": False}
    return next((copy.deepcopy(row) for row in controls.get("known_candidates", [])
                 if row["name"] == name), None)


def collect_selection_conflicts(
    job: dict, root: Path, value, controls: dict, *, previous: list[dict] | None = None,
) -> list[dict]:
    """Collect all exact collisions before a validator stops on its first bad row.

    Store the returned bounded list in current.context.selection_conflicts. The
    original output and packet remain immutable; these are only repair hints.
    Explicit reuse/revision rows remain subject to their existing validators.
    """
    result = copy.deepcopy(previous or [])
    if len(result) > MAX_CONFLICTS:
        raise ValueError("Selection conflict context exceeds the finite three-attempt contract")
    if not isinstance(value, dict) or not isinstance(value.get("skills"), list):
        return result
    for row in value["skills"][:8]:
        if not isinstance(row, dict) or "reuse_candidate_id" in row or "revise_candidate_id" in row:
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name or len(name) > 64:
            continue
        authority = _authority(job, root, name, controls)
        if authority is None or all(row.get(key) == authority[key] for key in WORKFLOW_FIELDS):
            continue
        constraint = {"name": name, "candidate_id": authority["candidate_id"],
                      "metadata_sha256": metadata_sha256(authority)}
        previous_row = next((item for item in result if item["name"] == name), None)
        if previous_row is not None:
            if previous_row != constraint:
                raise ValueError("Existing workflow authority changed during a frozen selection window")
        else:
            result.append(constraint)
    if len(result) > MAX_CONFLICTS:
        raise ValueError("Selection conflict context exceeds the finite three-attempt contract")
    return result


def repair_selection_controls(
    job: dict, root: Path, controls: dict, context: dict, *, language_policy: dict,
    max_packet_chars: int = 32000,
) -> dict:
    """Reserve every accumulated conflicting name and fit exact authorities first.

    Assigned methods, source refs, and input IDs are copied exactly. Only readonly
    summaries/retrieval hints may be omitted. A missing full authority is marked
    unavailable for reuse; its name remains reserved, so renaming cannot regress.
    """
    result = copy.deepcopy(controls)
    diagnostic = _coverage_hint(context.get("planning_coverage"), controls)
    # A later valid response must not inherit a previous attempt's stale defect.
    result.pop("planning_coverage", None)
    result.pop("planning_coverage_repair_rule", None)
    if diagnostic is not None:
        result["planning_coverage"] = diagnostic
        result["planning_coverage_repair_rule"] = (
            "planning_coverage describes the previous response's exact item dispositions, not its input_ids header. "
            "Return every assigned items[].item_id exactly once across derived_from. Supply an explicit supported "
            "disposition for each missing_item_id; choose one supported destination for each duplicate_item_id; "
            "remove unknown_item_ids. Keep the complete exact input_ids header. Do not invent methods, automatically "
            "defer omitted items, alter canonical metadata or discard an item to make this diagnostic disappear."
        )
    constraints = context.get("selection_conflicts", [])
    if len(constraints) > MAX_CONFLICTS:
        raise ValueError("Selection conflict context exceeds the finite three-attempt contract")
    if not constraints:
        if diagnostic is None:
            return result
        # Keep the exact assignment and diagnostics; omit only readonly context
        # if adding repair notes would exceed the actual serialized packet.
        def coverage_fits() -> bool:
            try:
                planning.build_stage_packet("plan", "selection.json", controls=result,
                                            language_policy=language_policy, max_chars=max_packet_chars)
            except ValueError:
                return False
            return True

        if coverage_fits():
            return result
        for field in ("related_facts", "provisional_gaps"):
            if result.get(field):
                result[field] = []
                result["context_omitted"] = True
        while not coverage_fits() and result.get("known_candidates"):
            result["known_candidates"].pop()
            result["known_candidates_omitted"] = True
        if not coverage_fits():
            raise ValueError("Assigned methods and exact coverage repair notes exceed the bounded selection packet")
        return result
    result["reserved_candidate_names"] = [row["name"] for row in constraints]
    result["selection_conflicts"] = copy.deepcopy(constraints)
    result["selection_repair_rule"] = (
        "Every reserved_candidate_name belongs to an existing canonical workflow. Do not reuse any reserved "
        "name for different task/trigger/inputs/outputs/boundary, including earlier-attempt conflicts. "
        "If it is the same complete workflow and its full identity is in known_candidates, explicitly use "
        "{derived_from:[assigned method IDs],reuse_candidate_id:exact candidate_id,reason:reason}. "
        "Otherwise give the distinct workflow its own name. Never silently merge business fields, invent "
        "new method IDs, or drop an assigned method. Metadata is not original Evidence."
    )
    optional = result.get("known_candidates", [])
    result["known_candidates"] = []
    result["known_candidates_omitted"] = bool(result.get("known_candidates_omitted"))
    for field in ("related_facts", "provisional_gaps"):
        if result.get(field):
            result[field] = []
            result["context_omitted"] = True

    def fits() -> bool:
        try:
            planning.build_stage_packet("plan", "selection.json", controls=result,
                                        language_policy=language_policy, max_chars=max_packet_chars)
        except ValueError:
            return False
        return True

    if not fits():
        raise ValueError("Assigned methods and cumulative identity constraints exceed the bounded selection packet")
    loaded = set()
    for constraint in reversed(constraints):
        authority = _authority(job, root, constraint["name"], controls)
        if (authority is None or authority["candidate_id"] != constraint["candidate_id"]
                or metadata_sha256(authority) != constraint["metadata_sha256"]):
            raise ValueError("A repair identity must match the exact frozen canonical workflow")
        if len(result["known_candidates"]) < 8:
            result["known_candidates"].append(authority)
            if fits():
                loaded.add(authority["candidate_id"])
                continue
            result["known_candidates"].pop()
        result["known_candidates_omitted"] = True
    for authority in optional:
        if authority["candidate_id"] in loaded:
            continue
        if len(result["known_candidates"]) >= 8:
            result["known_candidates_omitted"] = True
            break
        result["known_candidates"].append(authority)
        if fits():
            loaded.add(authority["candidate_id"])
        else:
            result["known_candidates"].pop()
            result["known_candidates_omitted"] = True
    # Do not add a final flag after the budget check: the exact published JSON,
    # including all omission metadata, must fit the same limit used by the host.
    planning.build_stage_packet("plan", "selection.json", controls=result,
                                language_policy=language_policy, max_chars=max_packet_chars)
    return result
