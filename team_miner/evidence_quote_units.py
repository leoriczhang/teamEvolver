"""Immutable, continuous original-source units for ID-based Evidence citations."""

from __future__ import annotations

import copy
import hashlib
import json

QUOTE_UNIT_CHARS = 768
QUOTE_PROTOCOL = "source-quote-ids.v1"
QUOTE_STAGE = "evidence_review_quotes"
QUOTE_SCHEMA = "skill-miner.evidence-review-quotes.v1"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def quote_unit_count(metadata: dict) -> int:
    length = metadata["end_char"] - metadata["start_char"]
    return max(1, (length + QUOTE_UNIT_CHARS - 1) // QUOTE_UNIT_CHARS)


def source_quote_unit(original: dict, position: int) -> dict:
    """Slice Unicode characters without cleaning, reordering or repeating original content."""
    if type(position) is not int or not 0 <= position < quote_unit_count(original):
        raise ValueError("Source quote unit position is outside its frozen original chunk")
    relative = position * QUOTE_UNIT_CHARS
    content = original["content"][relative : relative + QUOTE_UNIT_CHARS]
    start = original["start_char"] + relative
    end, digest = start + len(content), _sha(content)
    return {
        "id": "src-" + _sha(_json([original["source_uri"], start, end, digest]))[:24],
        "source_uri": original["source_uri"],
        "start_char": start,
        "end_char": end,
        "sha256": digest,
        "content": content,
        "source_sha256": original.get("source_sha256", original["sha256"]),
        "ordinal": original["ordinal"],
        "original_chunk_id": original["id"],
        "original_chunk_sha256": original["sha256"],
    }


def validate_source_quote_unit(unit: dict, original: dict) -> None:
    relative = unit.get("start_char", -1) - original["start_char"]
    if relative < 0 or relative % QUOTE_UNIT_CHARS:
        raise ValueError("Source quote unit does not follow its immutable partition")
    expected = source_quote_unit(original, relative // QUOTE_UNIT_CHARS)
    if unit != expected or _sha(original["content"]) != original["sha256"]:
        raise ValueError("Source quote unit differs from its frozen original slice")


def resolve_quote_ids(value, *, evidence: list[dict], target_schema: str) -> tuple[dict, list[dict]]:
    """Resolve only exact assigned IDs to raw source text; model text never becomes a quote."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "input_ids", "reviews"}
        or value["schema_version"] != QUOTE_SCHEMA
        or not isinstance(value["reviews"], list)
    ):
        raise ValueError("Source quote reviews must use their exact declared schema")
    by_id = {chunk["id"]: chunk for chunk in evidence}
    if len(by_id) != len(evidence):
        raise ValueError("Assigned source quote IDs must be unique")
    checked, audit = copy.deepcopy(value), []
    checked["schema_version"] = target_schema
    for row in checked["reviews"]:
        if not isinstance(row, dict) or "supporting_quote_ids" not in row or "supporting_quotes" in row:
            raise ValueError("A source quote review must select IDs instead of writing quotation text")
        selected = row.pop("supporting_quote_ids")
        if (
            not isinstance(selected, list)
            or len(selected) > 4
            or any(not isinstance(identity, str) or identity not in by_id for identity in selected)
            or len(selected) != len(set(selected))
        ):
            raise ValueError("Supporting quote IDs must be unique exact IDs from this assigned Evidence packet")
        row["supporting_quotes"] = []
        for index, identity in enumerate(selected):
            unit = by_id[identity]
            if len(unit["content"]) > 800 or unit["sha256"] != _sha(unit["content"]):
                raise ValueError("Resolved source quotations must retain their exact bounded raw content")
            row["supporting_quotes"].append({"source_uri": unit["source_uri"], "text": unit["content"]})
            audit.append(
                {
                    "item_id": row.get("item_id"),
                    "quote_index": index,
                    "source_quote_id": identity,
                    "source_uri": unit["source_uri"],
                    "start_char": unit["start_char"],
                    "end_char": unit["end_char"],
                    "sha256": unit["sha256"],
                    "original_ordinal": unit.get("ordinal"),
                    "original_chunk_id": unit.get("original_chunk_id"),
                }
            )
    return checked, audit
