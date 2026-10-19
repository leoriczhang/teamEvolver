"""Per-packet short identities for frozen original-source Evidence review."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Any

from .evidence_quote_units import QUOTE_SCHEMA, QUOTE_STAGE

PROTOCOL = "te-evidence-quote-aliases.v1"
_FIELDS = {"schema_version", "quotes", "methods", "canonical_packet_sha256", "aliased_packet_sha256",
           "manifest_sha256"}
_REVIEW_FIELDS = {"item_id", "outcome", "reason", "source_refs", "supporting_quote_ids", "partial_steps", "candidate"}


class QuoteAliasManifestError(ValueError):
    """The immutable host assignment or its sidecar is damaged."""


class QuoteAliasOutputError(ValueError):
    """The model used incomplete, unknown or wrong-role identities."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _ids(value: Any, *, output: bool = False, empty: bool = False) -> list[str]:
    error = QuoteAliasOutputError if output else QuoteAliasManifestError
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(identity, str) or not identity for identity in value)
            or len(value) != len(set(value))):
        raise error("Quote review identities must be exact unique strings in their declared role")
    return value


def _mapped(value: Any, mapping: dict, *, output: bool = False, empty: bool = False) -> list[str]:
    ids = _ids(value, output=output, empty=empty)
    if any(identity not in mapping for identity in ids):
        error = QuoteAliasOutputError if output else QuoteAliasManifestError
        raise error("Quote review uses an unknown or wrong-role identity; use only the current q/m assignment")
    return [mapping[identity] for identity in ids]


def _build(packet: Any) -> tuple[dict, dict]:
    if (not isinstance(packet, dict) or packet.get("stage") != QUOTE_STAGE
            or not isinstance(packet.get("derived_controls"), dict)):
        raise QuoteAliasManifestError("Quote aliases require an original-source quote review packet")
    controls = packet["derived_controls"]
    if controls.get("quote_alias_protocol") is not None:
        raise QuoteAliasManifestError("A canonical quote packet cannot already be aliased")
    evidence, items = packet.get("evidence"), controls.get("items")
    if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 999
            or any(not isinstance(row, dict) for row in evidence)
            or not isinstance(items, list) or not 1 <= len(items) <= 8
            or any(not isinstance(row, dict) for row in items)):
        raise QuoteAliasManifestError("Quote aliases require bounded nonempty Evidence and method assignments")
    quote_ids = _ids([row.get("id") for row in evidence])
    method_ids = _ids([row.get("item_id") for row in items])
    if (packet.get("input_ids") != quote_ids
            or len(set(quote_ids + method_ids)) != len(quote_ids + method_ids)
            or any(re.fullmatch(r"[qm]\d{3}", identity) for identity in quote_ids + method_ids)):
        raise QuoteAliasManifestError("Canonical quote/method roles must be complete, separate and unaliased")
    quotes = {f"q{index:03d}": identity for index, identity in enumerate(quote_ids, 1)}
    methods = {f"m{index:03d}": identity for index, identity in enumerate(method_ids, 1)}
    qr, mr = ({value: key for key, value in mapping.items()} for mapping in (quotes, methods))
    result = copy.deepcopy(packet)
    result["input_ids"] = list(quotes)
    for row in result["evidence"]:
        row["id"] = qr[row["id"]]
    target = result["derived_controls"]
    target["quote_alias_protocol"] = PROTOCOL
    for row in target["items"]:
        row["item_id"] = mr[row["item_id"]]
    if "input_ids" in target:
        if controls["input_ids"] != quote_ids:
            raise QuoteAliasManifestError("Control quote assignment differs from the frozen Evidence")
        target["input_ids"] = list(quotes)
    if controls.get("source_unit_review_protocol"):
        operative = _ids(controls.get("method_input_ids"), empty=True)
        context = _ids(controls.get("context_input_ids"), empty=True)
        if set(operative) & set(context) or set(operative + context) != set(quote_ids):
            raise QuoteAliasManifestError("Exact review method/context roles must partition the assigned quotes")
        target["method_input_ids"] = _mapped(operative, qr, empty=True)
        target["context_input_ids"] = _mapped(context, qr, empty=True)
    support = target.get("support_units", [])
    if not isinstance(support, list) or any(not isinstance(unit, dict) for unit in support):
        raise QuoteAliasManifestError("Support units must retain their declared quote assignments")
    for unit in support:
        unit["quote_ids"] = _mapped(unit.get("quote_ids"), qr)
    manifest = {"schema_version": PROTOCOL, "quotes": quotes, "methods": methods,
                "canonical_packet_sha256": _digest(packet), "aliased_packet_sha256": _digest(result)}
    manifest["manifest_sha256"] = _digest(manifest)
    return result, manifest


def build_quote_alias_packet(packet: Any) -> tuple[dict, dict]:
    """Alias only declared identity fields, preserving all original content and provenance."""
    try:
        result, manifest = _build(packet)
        validate_quote_alias_manifest(manifest, packet=result)
        return result, manifest
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, QuoteAliasManifestError):
            raise
        raise QuoteAliasManifestError("Canonical quote assignment cannot be aliased") from exc


def validate_quote_alias_manifest(
    manifest: Any, *, packet: dict | None = None, canonical_packet: dict | None = None,
) -> None:
    """Check role bijections and frozen packet digests; the host also binds the persisted sidecar SHA."""
    try:
        if not isinstance(manifest, dict) or set(manifest) != _FIELDS or manifest["schema_version"] != PROTOCOL:
            raise QuoteAliasManifestError("Invalid frozen quote alias manifest")
        for field in ("canonical_packet_sha256", "aliased_packet_sha256", "manifest_sha256"):
            if not isinstance(manifest[field], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[field]):
                raise QuoteAliasManifestError("Quote manifest requires exact SHA-256 digests")
        if _digest({k: v for k, v in manifest.items() if k != "manifest_sha256"}) != manifest["manifest_sha256"]:
            raise QuoteAliasManifestError("Frozen quote manifest SHA differs")
        canonical_ids = []
        for field, prefix, maximum in (("quotes", "q", 999), ("methods", "m", 8)):
            mapping = manifest[field]
            if (not isinstance(mapping, dict) or not 1 <= len(mapping) <= maximum
                    or list(mapping) != [f"{prefix}{index:03d}" for index in range(1, len(mapping) + 1)]):
                raise QuoteAliasManifestError("Quote aliases must be contiguous within their exact role")
            identities = _ids(list(mapping.values()))
            if any(re.fullmatch(r"[qm]\d{3}", identity) for identity in identities):
                raise QuoteAliasManifestError("Canonical quote identities cannot themselves be short aliases")
            canonical_ids.extend(identities)
        if len(canonical_ids) != len(set(canonical_ids)):
            raise QuoteAliasManifestError("Quote and method roles must be independent bijections")
        if packet is not None:
            if _digest(packet) != manifest["aliased_packet_sha256"]:
                raise QuoteAliasManifestError("Frozen aliased quote packet SHA differs")
            controls = packet["derived_controls"]
            if (packet.get("stage") != QUOTE_STAGE or controls.get("quote_alias_protocol") != PROTOCOL
                    or packet["input_ids"] != list(manifest["quotes"])
                    or [row["id"] for row in packet["evidence"]] != list(manifest["quotes"])
                    or [row["item_id"] for row in controls["items"]] != list(manifest["methods"])
                    or ("input_ids" in controls and controls["input_ids"] != list(manifest["quotes"]))):
                raise QuoteAliasManifestError("Frozen quote packet has different assigned roles")
            for unit in controls.get("support_units", []):
                _mapped(unit["quote_ids"], manifest["quotes"])
        if canonical_packet is not None:
            expected_packet, expected_manifest = _build(canonical_packet)
            if expected_manifest != manifest or (packet is not None and expected_packet != packet):
                raise QuoteAliasManifestError("Frozen quote mapping differs from its canonical original packet")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, QuoteAliasManifestError):
            raise
        raise QuoteAliasManifestError("Malformed frozen quote alias metadata") from exc


def decode_quote_alias_output(raw: Any, manifest: dict) -> dict:
    """Restore exact assigned identities; never guess, rewrite business text or infer source facts."""
    validate_quote_alias_manifest(manifest)
    try:
        if (not isinstance(raw, dict) or len(_json(raw)) > 12000
                or set(raw) != {"schema_version", "input_ids", "reviews"}
                or raw["schema_version"] != QUOTE_SCHEMA or not isinstance(raw["reviews"], list)):
            raise QuoteAliasOutputError("Aliased quote review must use its exact schema within 12000 characters")
        result = copy.deepcopy(raw)
        header = _ids(result["input_ids"], output=True)
        result["input_ids"] = _mapped(header, manifest["quotes"], output=True)
        if set(header) != set(manifest["quotes"]):
            raise QuoteAliasOutputError("input_ids must contain every assigned q alias exactly once")
        consumed = []
        for row in result["reviews"]:
            if not isinstance(row, dict) or set(row) != _REVIEW_FIELDS:
                raise QuoteAliasOutputError("Quote review rows must contain exactly their declared fields")
            identity = row["item_id"]
            if not isinstance(identity, str) or identity not in manifest["methods"]:
                raise QuoteAliasOutputError("Review item_id must be an exact assigned m alias")
            consumed.append(identity)
            row["item_id"] = manifest["methods"][identity]
            if not isinstance(row["supporting_quote_ids"], list) or len(row["supporting_quote_ids"]) > 4:
                raise QuoteAliasOutputError("A review may select at most four assigned q aliases")
            row["supporting_quote_ids"] = _mapped(
                row["supporting_quote_ids"], manifest["quotes"], output=True, empty=True,
            )
        counts = Counter(consumed)
        if set(counts) != set(manifest["methods"]) or any(count != 1 for count in counts.values()):
            raise QuoteAliasOutputError("Every assigned m alias must be reviewed exactly once; no partial result")
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, QuoteAliasOutputError):
            raise
        raise QuoteAliasOutputError("Malformed aliased quote review output") from exc
