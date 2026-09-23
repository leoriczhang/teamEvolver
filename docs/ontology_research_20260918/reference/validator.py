"""Limited reference invariants for synthetic contracts; NOT a production security service.

The registry and observation inputs represent trusted server-side adapters in tests.
This file does not authenticate users, verify signatures, access databases, or call LLMs.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]

class ContractError(ValueError):
    """A draft contract or one of its tested invariants is invalid."""

def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)

def validate_shape(name: str, value: Any) -> None:
    schema = load_json(ROOT / "contracts" / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
                    key=lambda error: str(list(error.absolute_path)))
    if errors:
        first = errors[0]
        raise ContractError(f"{name}:{list(first.absolute_path)}: {first.message}")

def _index(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row[key] in indexed:
            raise ContractError(f"duplicate {key}: {row[key]}")
        indexed[row[key]] = row
    return indexed

def _instant(text: str) -> datetime:
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ContractError("timestamp requires a timezone")
    return result

def validate_candidate(bundle: dict[str, Any], *, expected_tenant: str,
                       registry: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate the small inline demo subset using an independently supplied registry.

    Real code must obtain registry records from an authenticated source catalog. Never
    build the trusted registry from untrusted candidate bytes in a production service.
    """
    validate_shape("candidate_bundle", bundle)
    if bundle["tenant"] != expected_tenant:
        raise ContractError("tenant mismatch")
    if bundle["schema_revision"] != "sf-core@1.0.0":
        raise ContractError("schema revision is not approved in this demonstration")
    entities = _index(bundle["entities"], "entity_id")
    evidence = _index(bundle["evidence"], "evidence_id")
    supports = _index(bundle["support_sets"], "support_set_id")
    _index(bundle["assertions"], "assertion_id")
    allowed_types = {"IssueDefinition", "EvidenceSlot"}
    for entity in entities.values():
        if entity["entity_type"] not in allowed_types:
            raise ContractError("unknown entity type; propose a Schema change instead")
    for ev in evidence.values():
        trusted = registry.get(ev["evidence_id"])
        if not trusted or trusted.get("tenant") != expected_tenant:
            raise ContractError("unknown or foreign source evidence")
        for key in ("source_id", "source_revision", "segment_id", "source_sha256", "source_uri", "source_kind"):
            if trusted.get(key) != ev[key]:
                raise ContractError(f"source registry mismatch: {key}")
        if trusted.get("status") != "active" or ev["status"] != "active":
            raise ContractError("revoked evidence")
    for support in supports.values():
        refs = support["evidence_ids"]
        if len(set(refs)) != len(refs):
            raise ContractError("duplicate evidence in support set")
        if any(ref not in evidence for ref in refs):
            raise ContractError("support set references missing evidence")
    for a in bundle["assertions"]:
        if a["subject"] not in entities or a["object"] not in entities:
            raise ContractError("dangling entity endpoint")
        if a["predicate"] != "requires_evidence":
            raise ContractError("predicate not allowed by the demo Schema")
        if entities[a["subject"]]["entity_type"] != "IssueDefinition" or entities[a["object"]]["entity_type"] != "EvidenceSlot":
            raise ContractError("predicate domain/range mismatch")
        if a["valid_to"] is not None and _instant(a["valid_from"]) >= _instant(a["valid_to"]):
            raise ContractError("empty or reversed half-open valid-time interval")
        ids: list[str] = []
        for support_id in a["support_set_ids"]:
            if support_id not in supports:
                raise ContractError("unknown support set")
            ids.extend(supports[support_id]["evidence_ids"])
        kinds = {evidence[eid]["source_kind"] for eid in ids}
        if a["epistemic_kind"] == "system_fact" and kinds != {"system_record"}:
            raise ContractError("system fact requires independently registered system observation evidence")
        if a["epistemic_kind"] == "document_fact" and "user_claim" in kinds:
            raise ContractError("user claim cannot certify a document fact")

def visible_for_demo(bundle: dict[str, Any], assertion: dict[str, Any], *,
                     principal_tenant: str, entity_grants: set[str], evidence_grants: set[str],
                     current_registry: Mapping[str, Mapping[str, Any]],
                     authority_available: bool = True) -> bool:
    """Pure visibility model, NOT a replacement for IAM, current auth leases or RLS."""
    if not authority_available or principal_tenant != bundle["tenant"]:
        return False
    if not {assertion["subject"], assertion["object"]}.issubset(entity_grants):
        return False
    supports = {s["support_set_id"]: s for s in bundle["support_sets"]}
    for support_id in assertion["support_set_ids"]:
        support = supports.get(support_id)
        if not support:
            return False
        for eid in support["evidence_ids"]:
            record = current_registry.get(eid, {})
            if eid not in evidence_grants or record.get("status") != "active" or record.get("tenant") != principal_tenant:
                return False
    return True

def eval_eq(slot: str, expected: Any, observations: Mapping[str, Mapping[str, Any]]) -> bool | None:
    """An observation must have been authenticated before being passed to this function."""
    observation = observations.get(slot)
    if not observation or observation.get("verified") is not True or observation.get("fresh") is not True or observation.get("conflict") is True:
        return None
    # In production, type equality is determined by the approved slot Schema.
    if type(observation.get("value")) is not type(expected):
        return None
    return observation["value"] == expected

def kleene_all(values: list[bool | None]) -> bool | None:
    if not values:  # An empty evidence list must not create a vacuous approval.
        return None
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True

def pack_complete_units(units: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], bool]:
    """Pack premeasured atomic fact/qualifier/proof units, without estimating real tokens."""
    if type(budget) is not int or budget < 0:
        raise ContractError("budget must be a nonnegative integer")
    output: list[dict[str, Any]] = []
    truncated = False
    used = 0
    for unit in units:
        cost = unit.get("token_cost")
        if type(cost) is not int or cost <= 0:
            raise ContractError("each complete unit requires a positive measured token cost")
        if used + cost <= budget:
            output.append(deepcopy(unit))
            used += cost
        else:
            truncated = True
    return output, truncated
