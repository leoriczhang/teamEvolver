"""Exact source ownership for review; transport chunks never define business membership."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .source_unit_scope import (
    _content_ids,
    _digest,
    _method_ids,
    _ordered,
    accepted_source_classification,
    owned_source_context,
)
from .source_units import PROTOCOL, load_source_unit

REVIEW_PROTOCOL = "te-exact-source-review.v1"


def prepare_routing(root: Path, state: dict, *, write: bool = True) -> dict:
    from .ov_staged_orchestration import _read, _save

    records, shared, uncertain = accepted_source_classification(root, state)
    value = {"protocol": REVIEW_PROTOCOL, "source_unit_protocol": PROTOCOL,
             "chunk_count": state["chunk_count"], "source_unit_count": state["source_unit_count"],
             "accepted_units_sha256": _digest(records), "coverage_complete": True,
             "shared_unit_ids": _ordered(root, shared),
             "owned_context_groups": owned_source_context(root, records),
             "uncertain_unit_groups": [_ordered(root, group) for group in uncertain],
             "routing_is_evidence": False}
    path = root / "source-routing/exact-context.json"
    if path.exists() and _read(path) != value:
        raise ValueError("Frozen exact source review routing changed after the complete scan")
    if write:
        _save(path, value)
    return value


def method_scope(root: Path, record: dict, routing: dict) -> dict:
    from .ov_staged_orchestration import _read

    state = _read(root / "state.json")
    if prepare_routing(root, state, write=False) != routing:
        raise ValueError("Exact review ownership differs from the accepted source scan")
    bindings = record.get("source_unit_bindings")
    if not isinstance(bindings, dict):
        raise ValueError("Exact review method lost its accepted source unit membership")
    operative = _method_ids(root, {identity for ref in record["item"]["source_refs"] for identity in bindings[ref]})
    if not operative:
        raise ValueError("Exact Evidence review needs original method text")
    context = set(routing["shared_unit_ids"])
    for identity in operative:
        context.update(load_source_unit(root, identity)["context_unit_ids"])
    for group in routing["owned_context_groups"]:
        if operative.intersection(group["method_unit_ids"]):
            context.update(group["context_unit_ids"])
    for group in routing["uncertain_unit_groups"]:
        if operative.intersection(group):
            context.update(group)
    context = _content_ids(root, context) - operative
    identities = _ordered(root, operative | context)
    rows = [load_source_unit(root, identity) for identity in identities]
    selected = {ordinal for row in rows for ordinal in row["ordinals"]}
    method_ordinals = {ordinal for identity in operative for ordinal in load_source_unit(root, identity)["ordinals"]}
    result = {"scope_protocol": "te-source-scope.v3", "source_unit_review_protocol": REVIEW_PROTOCOL,
              "source_unit_ids": identities, "operative_source_unit_ids": _ordered(root, operative),
              "context_source_unit_ids": _ordered(root, context), "ordinals": sorted(selected),
              "method_ordinals": sorted(method_ordinals), "routing_sha256": routing["accepted_units_sha256"],
              "routing_complete": True}
    result["source_unit_scope_sha256"] = _digest(result)
    return result


def quote_parent(root: Path, identity: str) -> dict:
    """Keep an exact row as the parent of bounded literal quotes, even across transport chunks."""
    row = deepcopy(load_source_unit(root, identity))
    row["source_unit_id"] = identity
    row["review_ordinals"] = row["ordinals"]
    row["ordinal"] = row["ordinals"][0]  # Transport hint only; source_unit_id is authoritative.
    return row


def validate_scope(root: Path, descriptor: dict) -> None:
    fields = ("scope_protocol", "source_unit_review_protocol", "source_unit_ids", "operative_source_unit_ids",
              "context_source_unit_ids", "ordinals", "method_ordinals", "routing_sha256", "routing_complete")
    if descriptor.get("source_unit_review_protocol") != REVIEW_PROTOCOL:
        raise ValueError("Exact Evidence review scope lost its protocol")
    projected = {key: descriptor[key] for key in fields}
    if _digest(projected) != descriptor.get("source_unit_scope_sha256"):
        raise ValueError("Frozen exact Evidence review scope changed")
    operative, context = set(descriptor["operative_source_unit_ids"]), set(descriptor["context_source_unit_ids"])
    ids = descriptor["source_unit_ids"]
    if (not operative or operative & context or len(set(ids)) != len(ids) or set(ids) != operative | context
            or ids != _ordered(root, ids)):
        raise ValueError("Exact Evidence method/context roles changed their original partition")
    rows = [load_source_unit(root, identity) for identity in ids]
    if sorted({ordinal for row in rows for ordinal in row["ordinals"]}) != descriptor["ordinals"]:
        raise ValueError("Exact Evidence review lost its complete transport archive")


def quote_roles(descriptor: dict, evidence: list[dict]) -> dict:
    if descriptor.get("source_unit_review_protocol") != REVIEW_PROTOCOL:
        return {}
    operative = set(descriptor["operative_source_unit_ids"])
    return {"source_unit_review_protocol": REVIEW_PROTOCOL,
            "method_input_ids": [row["id"] for row in evidence if row.get("source_unit_id") in operative],
            "context_input_ids": [row["id"] for row in evidence if row.get("source_unit_id") not in operative]}


def validate_positive_support(descriptor: dict, review: dict, audit: list[dict]) -> None:
    if descriptor.get("source_unit_review_protocol") != REVIEW_PROTOCOL or review["outcome"] != "candidate":
        return
    if not set(descriptor["operative_source_unit_ids"]).intersection(row.get("source_unit_id") for row in audit):
        raise ValueError("Context alone cannot support a complete original method during Evidence review")
