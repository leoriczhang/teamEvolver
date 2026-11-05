# ruff: noqa: F401
# Imports are part of the explicit compatibility surface consumed by split mixins.
"""Recheck provisional method deferrals against bounded, frozen original Evidence."""

from __future__ import annotations

import asyncio
import bisect
import copy
import hashlib
import json
import os
import tempfile
from itertools import chain, zip_longest
from pathlib import Path

from . import bounded_compile_plan as planning
from . import evidence_quote_aliases as quote_aliases
from . import evidence_quote_units as quote_units
from .ov_compile_mining import utc_now

REVIEW_SCHEMA = "skill-miner.evidence-review.v1"
RESULT_SCHEMA = "skill-miner.evidence-review-result.v1"
STAGE = "evidence_review"
MAX_OUTPUT_CHARS = 12000
_CURLY_QUOTES = {"“": '"', "”": '"', "„": '"', "‟": '"', "‘": "'", "’": "'", "‚": "'", "‛": "'"}
_STANDALONE_BULLETS = frozenset({"\uf0d8", "•", "‣", "◦", "▪", "⁃"})

REVIEW_INSTRUCTIONS = """Recheck only the assigned deferred method against original packet.evidence.
The proposed task, boundary and previous_deferral are unverified planning context, never Evidence.
Their summaries, related facts, and omission of a rule do not prove that the original lacks that rule.
TE scans the method's original ranges first, then every remaining range of all related original files.
This packet is only one fragment of that complete review. Do not infer whole-file absence from it.
Missing values for a particular future invocation (price, weight, cost, customer, date, region, or
other runtime parameters) are inputs to the method, not missing Evidence about its business rules.
A formula or decision procedure may be executable with user-supplied runtime inputs. Include all
needed symbolic inputs; never fabricate their values. Preserve numeric conditions, units and exceptions.
Return exactly schema_version, input_ids, reviews. schema_version is skill-miner.evidence-review.v1.
input_ids contains every exact TE packet.input_ids, never native OV runtime range IDs.
reviews contains exactly one row per assigned controls.items item_id, with exactly item_id, outcome,
reason, source_refs, supporting_quotes, partial_steps, candidate. Only assigned method IDs are allowed.
outcome is candidate, knowledge, or insufficient_this_fragment. reason is nonempty and <=1000 chars.
source_refs is a nonempty unique list of exact current packet source URIs from allowed_source_refs.
Every assigned original URI must remain accounted for. supporting_quotes is a list of at most four
{source_uri,text} objects; each quote is an exact, nonempty substring (<=800 chars) of current original
Evidence from that URI. Positive candidate/knowledge outcomes require at least one supporting quote.
partial_steps is a list of at most eight {text,quote_indexes} objects: text is nonempty <=400 chars,
quote_indexes is a nonempty list of unique indexes into supporting_quotes. Keep partial procedure
conditions found in an insufficient fragment, including numerical bounds and exceptions, with exact
quotes; do not discard them merely because the complete procedure spans other fragments.
When review_mode is combine, combine all supplied source-backed partial steps against the exact
original excerpts in packet.evidence. support_units are provisional controls, never new facts.
Preserve each distinct operative condition, input, exception and unresolved dependency. Do not vote
on fragment outcomes or mistake a partial formula for a complete method. TE combines bounded units
hierarchically; keep useful partial steps and quotes when the current combined unit is still insufficient.
For candidate, candidate contains exactly name, task, trigger, inputs, outputs, boundary, rationale;
TE adds verified provenance. Use a safe lowercase kebab name <=64 chars; task, trigger, boundary,
rationale are nonempty <=400 chars; inputs/outputs are 1-16 nonempty strings <=240 chars each.
Business attributes must be grounded in the current raw Evidence, not merely repeated from the proposal.
Select candidate only when the raw fragment supports the complete assigned reusable decision,
calculation or procedure, with its required conditions and inputs. knowledge means the assigned method
is actually factual lookup without an executable procedure; explain this using the raw material.
For knowledge or insufficient_this_fragment, candidate is null. When this fragment cannot support
complete execution, use insufficient_this_fragment with a concrete explanation, not a whole-file
missing-Evidence claim. TE alone determines a final deferral after all related fragments were reviewed.
Do not add methods, IDs, side files or human questions. Complete JSON remains <=12000 chars.
The sole relative output path is evidence-review.json.
"""

QUOTE_REVIEW_INSTRUCTIONS = (
    REVIEW_INSTRUCTIONS.replace(REVIEW_SCHEMA, quote_units.QUOTE_SCHEMA)
    .replace(
        "source_refs, supporting_quotes, partial_steps, candidate",
        "source_refs, supporting_quote_ids, partial_steps, candidate",
    )
    .replace(
        "Every assigned original URI must remain accounted for. supporting_quotes is a list of at most four\n"
        "{source_uri,text} objects; each quote is an exact, nonempty substring (<=800 chars) of current original\n"
        "Evidence from that URI. Positive candidate/knowledge outcomes require at least one supporting quote.",
        "Every assigned original URI must remain accounted for. supporting_quote_ids is a list of at most four\n"
        "unique exact IDs from current packet.evidence. Each Evidence unit is continuous original text, at most\n"
        "768 characters. Select its ID; never copy, clean, reorder or reconstruct quote text or table cells.\n"
        "TE resolves these IDs to the exact original source offsets and text. Positive outcomes need at least one ID.",
    )
    .replace("indexes into supporting_quotes", "indexes into supporting_quote_ids")
)

QUOTE_ALIAS_INSTRUCTIONS = """
This packet uses te-evidence-quote-aliases.v1. Only packet.evidence[].id q001, q002, ... are
selectable quote identities. Only controls.items[].item_id m001, m002, ... are review method identities.
Copy every current q identity exactly once into input_ids, and review every assigned m identity
exactly once. supporting_quote_ids selects at most four exact current q identities, without duplicates.
Never use m identities as quotes, q identities as methods, original_chunk_id, hashes, native runtime
IDs, previous packet IDs or IDs mentioned inside original text. Do not invent or reconstruct any ID.
Keep exact source URIs in source_refs and all business conditions unchanged; short identities are
host bookkeeping, not facts. TE restores them to immutable original offsets, hashes and text.
Before describing a missing step or rule, inspect ALL original units in this current assignment.
Not selecting a unit in supporting_quote_ids does not mean that its original content is absent.
An unselected step present in another current unit is still supplied Evidence. Check it and choose
its q identity when needed. Do not infer missing original text from your chosen representative quotes.
If the assigned original units genuinely cannot support a complete procedure, state the concrete
insufficiency within this fragment honestly; do not fabricate facts or assert whole-source absence.
"""


def _review_instructions(state: dict, *, quote_ids: bool = False) -> str:
    """New routed reviews describe their assigned scope; legacy compiler bytes stay unchanged."""
    from .source_scope import EFFICIENCY_PROTOCOL

    rules = QUOTE_REVIEW_INSTRUCTIONS if quote_ids else REVIEW_INSTRUCTIONS
    if state.get("efficiency_protocol") != EFFICIENCY_PROTOCOL:
        return rules
    return rules.replace(
        "TE scans the method's original ranges first, then every remaining range of all related original files.\n"
        "This packet is only one fragment of that complete review. Do not infer whole-file absence from it.",
        "TE reviews the assigned routed original method ranges and shared constraints. The complete source\n"
        "was indexed to select these ranges; that routing index is not original Evidence or proof that no\n"
        "other relevant rule exists. This packet is one fragment of the routed review, not a review of every\n"
        "original range. Do not infer whole-file or whole-source absence from it.",
    ).replace(
        "TE alone determines a final deferral after all related fragments were reviewed.",
        "TE alone determines a final deferral after every assigned routed fragment was reviewed.\n"
        "Explain any insufficiency within that reviewed scope; do not claim the complete source lacks a rule.",
    )


def _routed_review_scope(descriptor: dict, *, complete: bool = False) -> dict:
    from .source_scope import SOURCE_SCOPE_PROTOCOLS

    if descriptor.get("scope_protocol") not in SOURCE_SCOPE_PROTOCOLS:
        return {}
    result = {
        "scope_protocol": descriptor["scope_protocol"],
        "review_scope": "routed_original_ranges",
        "routed_scope_chunk_count": len(descriptor["ordinals"]),
        "all_original_ranges_reviewed": False,
    }
    if complete:
        result["all_routed_ranges_reviewed"] = True
    if descriptor.get("source_unit_review_protocol"):
        result.update(source_unit_review_protocol=descriptor["source_unit_review_protocol"],
                      review_scope="exact_original_units", routed_source_unit_count=len(descriptor["source_unit_ids"]))
    return result


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read(path: Path):
    if path.is_symlink():
        raise ValueError("Evidence review cache must not be a symbolic link")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".evidence-review-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_immutable(path: Path, value) -> None:
    if path.exists():
        if _read(path) != value:
            raise ValueError("A frozen Evidence review result changed during recovery")
        return
    _save(path, value)


def _exact_ids(value, expected: list[str], label: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
        or set(value) != set(expected)
    ):
        raise ValueError(f"{label} must contain every exact assigned ID")


def _quote_locator_projection(text: str) -> tuple[str, list[int]]:
    """Project formatting only, retaining the original offset of each searchable character."""
    projected, offsets = [], []
    position = 0
    for line in text.splitlines(keepends=True):
        if line.strip() not in _STANDALONE_BULLETS:
            for index, character in enumerate(line):
                if not character.isspace():
                    projected.append(_CURLY_QUOTES.get(character, character))
                    offsets.append(position + index)
        position += len(line)
    return "".join(projected), offsets


def _restore_supporting_quote(quote: dict, evidence: list[dict], item_id: str, quote_index: int) -> dict | None:
    """Treat a reformatted quote as a locator, never as newly accepted source text."""
    originals = [chunk for chunk in evidence if chunk["source_uri"] == quote["source_uri"]]
    proposed = quote["text"]
    if any(proposed in chunk["content"] for chunk in originals):
        return None
    projected, _ = _quote_locator_projection(proposed)
    matches = {}
    if projected:
        for chunk in originals:
            material, offsets = _quote_locator_projection(chunk["content"])
            cursor = 0
            while True:
                position = material.find(projected, cursor)
                if position < 0:
                    break
                start, end = offsets[position], offsets[position + len(projected) - 1] + 1
                source_start = chunk["start_char"]
                identity = (source_start + start, source_start + end)
                excerpt = chunk["content"][start:end]
                previous = matches.get(identity)
                if previous is not None and previous != excerpt:
                    raise ValueError("Frozen original excerpts disagree at the same source offset")
                matches[identity] = excerpt
                cursor = position + 1
    label = f"Supporting quote {quote_index} for {item_id}"
    hint = "; copy a short continuous Unicode excerpt from the current original Evidence"
    if not matches:
        raise ValueError(label + " must literally occur in current original Evidence" + hint)
    if len(matches) != 1:
        raise ValueError(label + " has ambiguous source offsets after formatting restoration" + hint)
    (start, end), excerpt = next(iter(matches.items()))
    if len(excerpt) > 800:
        raise ValueError(label + " restores to an original excerpt exceeding 800 characters" + hint)
    quote["text"] = excerpt
    return {
        "item_id": item_id,
        "quote_index": quote_index,
        "source_uri": quote["source_uri"],
        "start_char": start,
        "end_char": end,
        "sha256": _sha(excerpt),
        "model_quote_sha256": _sha(proposed),
        "normalization": "unicode_quotes_whitespace_standalone_bullets",
    }


def validate_evidence_review(
    value,
    *,
    items: list[dict],
    evidence: list[dict],
    restorations: list[dict] | None = None,
    _max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """Validate assignment/provenance and literal quotes without claiming semantic proof."""
    if not isinstance(value, dict) or len(_json(value)) > _max_output_chars:
        raise ValueError("Evidence review must be an object within 12000 characters")
    if set(value) != {"schema_version", "input_ids", "reviews"} or value["schema_version"] != REVIEW_SCHEMA:
        raise ValueError("Evidence review must use its exact declared schema")
    expected_items = [item["item_id"] for item in items]
    if not 1 <= len(items) <= 8 or len(set(expected_items)) != len(items):
        raise ValueError("Evidence review requires one to eight unique assigned methods")
    _exact_ids(value["input_ids"], [chunk["id"] for chunk in evidence], "input_ids")
    rows = value["reviews"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Evidence review rows must be objects")
    _exact_ids([row.get("item_id") for row in rows], expected_items, "review item_ids")
    allowed = list(dict.fromkeys(chunk["source_uri"] for chunk in evidence))
    checked = copy.deepcopy(value)
    restored_quotes = []
    accounted = set()
    for row in checked["reviews"]:
        if set(row) != {
            "item_id",
            "outcome",
            "reason",
            "source_refs",
            "supporting_quotes",
            "partial_steps",
            "candidate",
        }:
            raise ValueError("Evidence review row fields are incomplete")
        if row["outcome"] not in {"candidate", "knowledge", "insufficient_this_fragment"}:
            raise ValueError("A fragment review cannot emit a final deferral")
        if not isinstance(row["reason"], str) or not row["reason"].strip() or len(row["reason"]) > 1000:
            raise ValueError("Evidence review reason must be nonempty and at most 1000 characters")
        refs = row["source_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != len(set(refs))
            or not set(refs) <= set(allowed)
        ):
            raise ValueError("Evidence review references must be exact current original URIs")
        accounted.update(refs)
        quotes = row["supporting_quotes"]
        if not isinstance(quotes, list) or len(quotes) > 4:
            raise ValueError("Evidence review quotes must be a bounded list")
        for quote_index, quote in enumerate(quotes):
            if not isinstance(quote, dict) or set(quote) != {"source_uri", "text"}:
                raise ValueError("A supporting quote requires exact source_uri and text")
            text = quote["text"]
            if quote["source_uri"] not in refs or not isinstance(text, str) or not text.strip() or len(text) > 800:
                raise ValueError(f"Supporting quote {quote_index} requires an exact current URI and 1-800 characters")
            restored = _restore_supporting_quote(quote, evidence, row["item_id"], quote_index)
            if restored is not None:
                restored_quotes.append(restored)
        if row["outcome"] != "insufficient_this_fragment" and not quotes:
            raise ValueError("A positive Evidence review requires original supporting quotes")
        steps = row["partial_steps"]
        if not isinstance(steps, list) or len(steps) > 8:
            raise ValueError("Partial procedure steps must be a bounded list")
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"text", "quote_indexes"}:
                raise ValueError("Partial procedure steps require text and quote indexes")
            indexes = step["quote_indexes"]
            if (
                not isinstance(step["text"], str)
                or not step["text"].strip()
                or len(step["text"]) > 400
                or not isinstance(indexes, list)
                or not indexes
                or any(type(index) is not int or not 0 <= index < len(quotes) for index in indexes)
                or len(indexes) != len(set(indexes))
            ):
                raise ValueError("Partial procedure facts require exact supporting quote indexes")
        if row["outcome"] == "insufficient_this_fragment" and quotes and not steps:
            raise ValueError("An insufficient fragment must retain its source-backed partial steps")
        if row["outcome"] == "candidate":
            candidate = row["candidate"]
            if not isinstance(candidate, dict) or "source_refs" in candidate:
                raise ValueError("Candidate provenance is assigned by TE")
            candidate["source_refs"] = list(refs)
            planning.validate_plan(
                {"schema_version": planning.PLAN_SCHEMA, "skills": [candidate], "deferred": []},
                source_refs=refs,
            )
        elif row["candidate"] is not None:
            raise ValueError("Only a supported candidate outcome may include candidate attributes")
    if accounted != set(allowed):
        raise ValueError("Evidence review must account for every assigned original URI")
    if restorations is not None:
        restorations.extend(restored_quotes)
    return checked


def validate_quote_id_review(
    value, *, items: list[dict], evidence: list[dict], _max_output_chars: int = MAX_OUTPUT_CHARS,
) -> tuple[dict, list[dict]]:
    if not isinstance(value, dict) or len(_json(value)) > _max_output_chars:
        raise ValueError("Source quote review raw output must remain within 12000 characters")
    resolved, audit = quote_units.resolve_quote_ids(value, evidence=evidence, target_schema=REVIEW_SCHEMA)
    expansion = max(0, len(_json(resolved)) - len(_json(value)))
    checked = validate_evidence_review(
        resolved, items=items, evidence=evidence, _max_output_chars=_max_output_chars + expansion
    )
    return checked, audit


def _method_descriptor(root: Path, index: int, record: dict, routing: dict | None = None) -> dict | None:
    leaves = record["leaf_items"]
    decisions = [_read(root / "method-decisions" / f"{leaf}.json") for leaf in leaves]
    if not leaves or not all(decision["target"] == "deferred" for decision in decisions):
        return None
    reasons = list(dict.fromkeys(decision["reason"] for decision in decisions))
    if len(reasons) != 1:
        raise ValueError("A reviewed proposal must have an unambiguous prior disposition")
    item = record["item"]
    files = list(dict.fromkeys(item["source_files"]))
    full = sorted(
        {ordinal for uri in files for ordinal in _read(root / "source-files" / (_sha(uri) + ".json"))["ordinals"]}
    )
    method = sorted(set(record["ordinals"]))
    if not method or not set(method) <= set(full):
        raise ValueError("Deferred method provenance is outside its frozen original files")
    gaps = [
        gap
        for gap in _read(root / "plan.json")["deferred"]
        if gap["reason"] == reasons[0] and set(gap["source_refs"]) == set(files)
    ]
    if len(gaps) != 1:
        raise ValueError("Deferred method must match one exact original plan disposition")
    scope = {"ordinals": full, "method_ordinals": method}
    if routing is not None and routing.get("source_unit_protocol") == "te-exact-source-units.v1":
        from .source_unit_review import method_scope

        scope = method_scope(root, record, routing)
    elif routing is not None:
        from .source_scope import route_candidate_scope

        scope = route_candidate_scope(root, scope, routing)
    return {
        "proposal_index": index,
        "item": item,
        "leaf_items": leaves,
        "method_ordinals": method,
        **scope,
        "ordinals": (scope["ordinals"] if scope.get("source_unit_review_protocol") else
                     method + [ordinal for ordinal in scope["ordinals"] if ordinal not in method]),
        "previous_gap": copy.deepcopy(gaps[0]),
    }


def _prepare_review(root: Path, count: int) -> dict:
    manifest = root / "evidence-review" / "manifest.json"
    if manifest.exists():
        return _read(manifest)
    # No method was deferred: no source rescan, review Compile or Evidence gap is needed.
    # In particular, do not run a transport-chunk ownership validator on exact row assignments.
    deferred = [index for index in range(count) if _method_descriptor(
        root, index, _read(root / "proposals" / f"{index:08d}.json"),
    ) is not None]
    if not deferred:
        result = {"proposal_indexes": [], "maximum_groups": 0}
        _save_immutable(manifest, result)
        return result
    routing = None
    state_path = root / "state.json"
    if state_path.is_file():
        from .source_scope import EFFICIENCY_PROTOCOL, prepare_source_routing

        state = _read(state_path)
        if state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
            if state.get("source_unit_protocol") == "te-exact-source-units.v1":
                from .source_unit_review import prepare_routing

                routing = prepare_routing(root, state)
            else:
                routing = prepare_source_routing(root, state)
    indexes, groups = [], 0
    for index in deferred:
        record = _read(root / "proposals" / f"{index:08d}.json")
        descriptor = _method_descriptor(root, index, record, routing)
        if descriptor is None:
            continue
        _save_immutable(root / "evidence-review" / "methods" / f"{index:08d}.json", descriptor)
        indexes.append(index)
        # A conservative finite bound: each original chunk can require one review call.
        groups += len(descriptor["ordinals"])
    result = {"proposal_indexes": indexes, "maximum_groups": groups}
    _save_immutable(manifest, result)
    return result


def _review_assignment(root: Path, descriptor: dict, cursor: int, language_policy: dict):
    ordinals = descriptor["ordinals"]
    chunks = [_read(root / "chunks" / f"{ordinal:08d}.json") for ordinal in ordinals[cursor : cursor + 8]]
    if not chunks:
        raise ValueError("Evidence review raw range is empty")
    group = planning.pack_chunks(chunks)[0]
    item = descriptor["item"]
    while group:
        refs = list(dict.fromkeys(chunk["source_uri"] for chunk in group))
        controls = {
            "review_mode": "original",
            "items": [{key: item[key] for key in ("item_id", "task", "boundary")}],
            "allowed_source_refs": refs,
            "previous_deferral": descriptor["previous_gap"]["reason"],
            "fragment_index": cursor,
            "complete_scope_chunks": len(ordinals),
            "scope_rule": (
                "The full source was indexed once. Review the routed original method and shared constraints; "
                "this bounded fragment alone does not establish whole-source absence."
                if descriptor.get("scope_protocol") else
                "All related original files are scanned; this is one bounded original fragment."
            ),
            **_routed_review_scope(descriptor),
        }
        try:
            planning.build_stage_packet(
                STAGE,
                "evidence-review.json",
                evidence_chunks=group,
                controls=controls,
                language_policy=language_policy,
            )
            return group, controls, cursor + len(group)
        except ValueError as error:
            if "bounded input budget" not in str(error) or len(group) == 1:
                raise
            group.pop()
    raise ValueError("Evidence review assignment cannot fit its bounded packet")


def _exact_quote_manifest(root: Path, descriptor: dict) -> dict:
    from .source_unit_review import quote_parent, validate_scope

    validate_scope(root, descriptor)
    segments, count = [], 0
    for identity in descriptor["source_unit_ids"]:
        original = quote_parent(root, identity)
        size = quote_units.quote_unit_count(original)
        segments.append({"source_unit_id": identity, "ordinal": original["ordinal"],
                         "first_unit": count, "unit_count": size})
        count += size
    return {"segments": segments, "unit_count": count, "initial_cursor": 0,
            "source_unit_review_protocol": descriptor["source_unit_review_protocol"],
            "source_unit_scope_sha256": descriptor["source_unit_scope_sha256"]}


def _quote_manifest(root: Path, descriptor: dict, legacy_cursor: int) -> dict:
    path = root / "evidence-review" / "quote-manifests" / f"{descriptor['proposal_index']:08d}.json"
    if path.exists():
        value = _read(path)
        if descriptor.get("source_unit_review_protocol") and value != _exact_quote_manifest(root, descriptor):
            raise ValueError("Frozen exact source quote manifest lost original unit coverage")
        return value
    segments, count = [], 0
    if descriptor.get("source_unit_review_protocol"):
        if legacy_cursor:
            raise ValueError("Exact source review cannot replace already accepted transport-based fragments")
        value = _exact_quote_manifest(root, descriptor)
        _save_immutable(path, value)
        return value
    for ordinal in descriptor["ordinals"]:
        metadata = _read(root / "chunk-metadata" / f"{ordinal:08d}.json")
        size = quote_units.quote_unit_count(metadata)
        segments.append({"ordinal": ordinal, "first_unit": count, "unit_count": size})
        count += size
    initial = sum(row["unit_count"] for row in segments[:legacy_cursor])
    value = {"segments": segments, "unit_count": count, "initial_cursor": initial}
    _save_immutable(path, value)
    return value


def _quote_original(root: Path, segment: dict) -> dict:
    if segment.get("source_unit_id"):
        from .source_unit_review import quote_parent

        return quote_parent(root, segment["source_unit_id"])
    return _read(root / "chunks" / f"{segment['ordinal']:08d}.json")


def _quote_assignment(root: Path, descriptor: dict, manifest: dict, cursor: int, policy: dict, max_chars: int = 32000):
    if descriptor.get("source_unit_review_protocol"):
        if manifest != _exact_quote_manifest(root, descriptor):
            raise ValueError("Frozen exact source quote manifest changed its original scope")
    segments = manifest["segments"]
    starts = [row["first_unit"] for row in segments]
    current_ordinal, original, group = None, None, []
    item = descriptor["item"]
    controls = None
    for position in range(cursor, min(cursor + 32, manifest["unit_count"])):
        segment = segments[bisect.bisect_right(starts, position) - 1]
        original_key = segment.get("source_unit_id", segment["ordinal"])
        if current_ordinal != original_key:
            current_ordinal = original_key
            original = _quote_original(root, segment)
        unit = quote_units.source_quote_unit(original, position - segment["first_unit"])
        proposed = [*group, unit]
        next_controls = {
            "review_mode": "original",
            "items": [{key: item[key] for key in ("item_id", "task", "boundary")}],
            "allowed_source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in proposed)),
            "previous_deferral": descriptor["previous_gap"]["reason"],
            "fragment_index": cursor,
            "complete_scope_chunks": len(descriptor["ordinals"]),
            "complete_scope_quote_units": manifest["unit_count"],
            "quotation_protocol": quote_units.QUOTE_PROTOCOL,
            "scope_rule": "Each ID names one continuous original source unit. All units are scanned; "
            "this bounded packet does not prove whole-file absence.",
            **_routed_review_scope(descriptor),
        }
        if descriptor.get("scope_protocol"):
            next_controls["scope_rule"] = (
                "Each ID names one continuous original source unit within the assigned routed method and "
                "shared-constraint ranges. All units in this scope are reviewed; this packet does not prove "
                "whole-file or whole-source absence."
            )
        if descriptor.get("source_unit_review_protocol"):
            from .source_unit_review import quote_roles

            next_controls.update(quote_roles(descriptor, proposed))
            next_controls["scope_rule"] += (
                " Each quote remains inside one exact original row. method_input_ids support the assigned "
                "method; context_input_ids retain constraints and cannot create a different task or customer."
            )
        try:
            if len(_json({"evidence": proposed})) > 24000:
                raise ValueError("Stage packet exceeds its bounded input budget")
            planning.build_stage_packet(
                quote_units.QUOTE_STAGE,
                "evidence-review.json",
                evidence_chunks=proposed,
                controls=next_controls,
                language_policy=policy,
                max_chars=max_chars,
            )
        except ValueError as error:
            if "bounded input budget" not in str(error) or not group:
                raise
            break
        group, controls = proposed, next_controls
    if not group:
        raise ValueError("Source quote review has no pending original units")
    return group, controls, cursor + len(group)


def _selected_source_quotes(evidence: list[dict], audit: list[dict]) -> list[dict]:
    selected = {unit["id"]: unit for unit in evidence}
    result = []
    for record in audit:
        unit = copy.deepcopy(selected[record["source_quote_id"]])
        unit["review_ordinals"] = unit.get("review_ordinals", [unit["ordinal"]] if "ordinal" in unit else [])
        result.append(unit)
    return result


def _fragment_source_quotes(root: Path, fragment: dict) -> list[dict]:
    evidence = []
    for record in fragment["source_quote_audit"]:
        original = _quote_original(root, {**record, "ordinal": record["original_ordinal"]})
        relative = record["start_char"] - original["start_char"]
        if relative < 0 or relative % quote_units.QUOTE_UNIT_CHARS:
            raise ValueError("Accepted source quote no longer matches its original partition")
        unit = quote_units.source_quote_unit(original, relative // quote_units.QUOTE_UNIT_CHARS)
        quote_units.validate_source_quote_unit(unit, original)
        if any(
            record[key] != unit[field]
            for key, field in (
                ("source_quote_id", "id"),
                ("source_uri", "source_uri"),
                ("end_char", "end_char"),
                ("sha256", "sha256"),
                ("original_chunk_id", "original_chunk_id"),
            )
        ):
            raise ValueError("Accepted source quote changed its original identity or offsets")
        evidence.append(unit)
    return _selected_source_quotes(evidence, fragment["source_quote_audit"])


def _finish_method(
    root: Path, descriptor: dict, terminal: dict | None = None, language_policy: dict | None = None
) -> dict:
    index = descriptor["proposal_index"]
    folder = root / "evidence-review" / "fragments" / f"{index:08d}"
    quote_folder = root / "evidence-review" / "quote-fragments" / f"{index:08d}"
    candidate, knowledge, candidate_reason = None, None, ""
    reasons = []
    support, inputs, sources, groups, quote_count = set(), [], [], 0, 0
    quote_paths = sorted(quote_folder.glob("*.json"))
    # Only one bounded fragment response is loaded at a time.
    for path in [*sorted(folder.glob("*.json")), *quote_paths]:
        fragment = _read(path)
        row = fragment["review"]
        if "quotation_protocol" in fragment:
            quote_count += len(fragment["input_ids"])
        else:
            inputs.extend(fragment["input_ids"])
        groups += 1
        if row["reason"] not in reasons and len(reasons) < 3:
            reasons.append(row["reason"])
        if row["outcome"] == "candidate":
            candidate = candidate or row["candidate"]
            candidate_reason = candidate_reason or row["reason"]
            support.update(fragment["ordinals"])
            sources.extend(row["source_refs"])
        elif row["outcome"] == "knowledge":
            knowledge = knowledge or row["reason"]
    expected = (descriptor["source_unit_ids"] if descriptor.get("source_unit_review_protocol") else
                [_read(root / "chunk-metadata" / f"{ordinal:08d}.json")["id"] for ordinal in descriptor["ordinals"]])
    if quote_count:
        manifest = _read(root / "evidence-review" / "quote-manifests" / f"{index:08d}.json")

        def expected_quote_ids():
            for segment in manifest["segments"]:
                if segment["first_unit"] < manifest["initial_cursor"]:
                    continue
                original = _quote_original(root, segment)
                for position in range(segment["unit_count"]):
                    yield quote_units.source_quote_unit(original, position)["id"]

        actual_quotes = chain.from_iterable(_read(path)["input_ids"] for path in quote_paths)
        if any(actual != expected for actual, expected in zip_longest(actual_quotes, expected_quote_ids())):
            raise ValueError("Completed source quote unit coverage must contain every ordered original unit")
        for segment in manifest["segments"]:
            if segment["first_unit"] < manifest["initial_cursor"]:
                continue
            inputs.append(segment["source_unit_id"] if segment.get("source_unit_id") else
                          _read(root / "chunk-metadata" / f"{segment['ordinal']:08d}.json")["id"])
    _exact_ids(inputs, expected, "Completed Evidence review input_ids")
    if terminal is not None:
        row = terminal["review"]
        candidate = row["candidate"] if row["outcome"] == "candidate" else None
        candidate_reason = row["reason"]
        knowledge = row["reason"] if row["outcome"] == "knowledge" else None
        sources = list(row["source_refs"])
        support = set(terminal["ordinals"])
        reasons = [row["reason"]]
    outcome = "candidate" if candidate else "knowledge" if knowledge else "deferred"
    full_sources = list(
        dict.fromkeys(
            _read(root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"] for ordinal in descriptor["ordinals"]
        )
    )
    if candidate:
        candidate = copy.deepcopy(candidate)
        candidate["source_refs"] = full_sources
    chinese = language_policy is None or language_policy.get("language") == "zh-CN"
    if outcome == "candidate":
        reason = candidate_reason
    elif outcome == "knowledge":
        reason = knowledge
    else:
        routed = bool(_routed_review_scope(descriptor))
        if chinese:
            reason = (f"已复核路由范围内全部{len(expected)}个原始片段" if routed
                      else f"已扫描全部{len(expected)}个原始片段")
            reason += ("并完成该范围的跨片段支持综合复核" if routed else "并完成跨片段支持综合复核") if terminal else (
                "，该范围未提供完整方法的原文支持" if routed else "，未找到完整方法的原文支持"
            )
            reason += "：" + "；".join(reasons)
        else:
            reason = (f"Reviewed all {len(expected)} original fragments in the routed scope" if routed
                      else f"Reviewed all {len(expected)} original fragments")
            reason += (
                (" and completed the support review within that scope" if routed
                 else " and completed the cross-fragment support review")
                if terminal
                else ("; this scope did not support a complete method" if routed
                      else "; no original support for a complete method was found")
            )
            reason += ": " + "; ".join(reasons)
    if len(reason) > 400:
        if chinese:
            reason = reason[:370] + "…（完整说明见原文复核清单）"
        else:
            suffix = "... (Full explanations are in the original Evidence review ledger.)"
            reason = reason[: 400 - len(suffix)] + suffix
    result = {
        "schema_version": RESULT_SCHEMA,
        "item_id": descriptor["item"]["item_id"],
        "proposal_index": index,
        "leaf_items": descriptor["leaf_items"],
        "outcome": outcome,
        "candidate": candidate,
        "reason": reason,
        "source_refs": full_sources,
        "support_source_refs": list(dict.fromkeys(sources)),
        "ordinals": sorted(descriptor["ordinals"]),
        "method_ordinals": sorted(support) if candidate else descriptor["method_ordinals"],
        "previous_gap": descriptor["previous_gap"],
        "reviewed_input_ids": inputs,
        "total_groups": groups,
        **_routed_review_scope(descriptor, complete=True),
    }
    if quote_count:
        result["reviewed_quote_unit_count"] = quote_count
        result["quote_coverage_journal"] = str(quote_folder.relative_to(root))
        result["quotation_protocol"] = quote_units.QUOTE_PROTOCOL
    if descriptor.get("source_unit_review_protocol"):
        result.update(source_unit_review_protocol=descriptor["source_unit_review_protocol"],
                      source_unit_ids=descriptor["source_unit_ids"],
                      operative_source_unit_ids=descriptor["operative_source_unit_ids"])
    return result


def _quote_chunks(row: dict, evidence: list[dict]) -> list[dict]:
    """Retain literal original excerpts with exact offsets; summaries never become Evidence."""
    result, seen = [], set()
    for quote in row["supporting_quotes"]:
        original = next(
            chunk
            for chunk in evidence
            if chunk["source_uri"] == quote["source_uri"] and quote["text"] in chunk["content"]
        )
        start = original["start_char"] + original["content"].index(quote["text"])
        identity = (
            "src-"
            + _sha(
                _json(
                    [
                        quote["source_uri"],
                        start,
                        start + len(quote["text"]),
                        _sha(quote["text"]),
                    ]
                )
            )[:24]
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "id": identity,
                "source_uri": quote["source_uri"],
                "start_char": start,
                "end_char": start + len(quote["text"]),
                "sha256": _sha(quote["text"]),
                "content": quote["text"],
                "source_sha256": original.get("source_sha256", original["sha256"]),
                "review_ordinals": original.get("review_ordinals", [original["ordinal"]])
                if "ordinal" in original
                else original["review_ordinals"],
            }
        )
    return result


def _support_unit(
    row: dict, evidence: list[dict], identity: str, parents: list[str], *, quotes: list[dict] | None = None
):
    quotes = _quote_chunks(row, evidence) if quotes is None else quotes
    return {
        "id": "review-unit-" + _sha(identity)[:24],
        "review": row,
        "quotes": quotes,
        "parents": parents,
        "ordinals": sorted({ordinal for quote in quotes for ordinal in quote["review_ordinals"]}),
    }


def _prepare_synthesis(root: Path, descriptor: dict) -> dict:
    index = descriptor["proposal_index"]
    folder = root / "evidence-review" / "synthesis" / f"{index:08d}"
    path = folder / "manifest.json"
    if path.exists():
        return _read(path)
    count = 0
    paths = [
        *sorted((root / "evidence-review" / "fragments" / f"{index:08d}").glob("*.json")),
        *sorted((root / "evidence-review" / "quote-fragments" / f"{index:08d}").glob("*.json")),
    ]
    for fragment_path in paths:
        fragment = _read(fragment_path)
        if not fragment["review"]["supporting_quotes"]:
            continue
        evidence = [_read(root / "chunks" / f"{ordinal:08d}.json") for ordinal in fragment["ordinals"]]
        quotes = _fragment_source_quotes(root, fragment) if "quotation_protocol" in fragment else None
        unit = _support_unit(fragment["review"], evidence, str(fragment_path.relative_to(root)), [], quotes=quotes)
        _save_immutable(folder / "units" / "0" / f"{count:08d}.json", unit)
        count += 1
    value = {"unit_count": count, "maximum_layers": count.bit_length() + 1}
    _save_immutable(path, value)
    return value


def _synthesis_assignment(
    root: Path,
    descriptor: dict,
    layer: int,
    cursor: int,
    count: int,
    policy: dict,
    stage: str = STAGE,
    max_chars: int = 32000,
):
    folder = root / "evidence-review" / "synthesis" / f"{descriptor['proposal_index']:08d}" / "units"
    units = [_read(folder / str(layer) / f"{index:08d}.json") for index in range(cursor, min(cursor + 2, count))]
    evidence, seen = [], set()
    controls = {
        "review_mode": "combine",
        "items": [{key: descriptor["item"][key] for key in ("item_id", "task", "boundary")}],
        "support_units": [],
        "complete_scope_chunks": len(descriptor["ordinals"]),
        "all_original_ranges_reviewed": True,
        "scope_rule": "Combine exact original excerpts and their provisional procedure descriptions; "
        "these bounded units may still be part of a larger combination.",
        **_routed_review_scope(descriptor, complete=True),
    }
    if descriptor.get("scope_protocol"):
        controls["scope_rule"] += (
            " Every assigned routed original range has been reviewed, not every range of the complete source."
        )
    for unit in units:
        unit_quote_ids = []
        piece_indexes = {}
        for quote_index, quote in enumerate(unit["quotes"]):
            first_piece = len(unit_quote_ids)
            pieces = [quote]
            if stage == quote_units.QUOTE_STAGE and len(quote["content"]) > quote_units.QUOTE_UNIT_CHARS:
                pieces = []
                for offset in range(0, len(quote["content"]), quote_units.QUOTE_UNIT_CHARS):
                    piece = copy.deepcopy(quote)
                    piece["content"] = quote["content"][offset : offset + quote_units.QUOTE_UNIT_CHARS]
                    piece["start_char"] = quote["start_char"] + offset
                    piece["end_char"] = piece["start_char"] + len(piece["content"])
                    piece["sha256"] = _sha(piece["content"])
                    piece["id"] = (
                        "src-"
                        + _sha(_json([piece["source_uri"], piece["start_char"], piece["end_char"], piece["sha256"]]))[
                            :24
                        ]
                    )
                    pieces.append(piece)
            for piece in pieces:
                unit_quote_ids.append(piece["id"])
                if piece["id"] not in seen:
                    evidence.append(piece)
                    seen.add(piece["id"])
            piece_indexes[quote_index] = list(range(first_piece, len(unit_quote_ids)))
        row = unit["review"]
        if stage == quote_units.QUOTE_STAGE and len(row["supporting_quotes"]) != len(unit["quotes"]):
            # Legacy literal citations could repeat one excerpt; their stored support
            # units deduplicated that span. Keep each old step bound to that same span.
            piece_indexes = {
                index: list(
                    dict.fromkeys(
                        piece
                        for unit_index, quote in enumerate(unit["quotes"])
                        if quote["source_uri"] == old["source_uri"] and quote["content"] == old["text"]
                        for piece in piece_indexes[unit_index]
                    )
                )
                for index, old in enumerate(row["supporting_quotes"])
            }
            if any(not indexes for indexes in piece_indexes.values()):
                raise ValueError("Legacy quoted procedure step lost its original supporting span")
        proposal = {k: v for k, v in (row["candidate"] or {}).items() if k != "source_refs"}
        proposal_excerpt = {
            key: value[:240] for key, value in proposal.items() if key in {"name", "task", "boundary", "trigger"}
        }
        context = {
            "unit_id": unit["id"],
            "partial_steps": row["partial_steps"],
            "candidate_proposal": proposal,
            "reason": row["reason"],
            "quote_ids": unit_quote_ids,
        }
        if stage == quote_units.QUOTE_STAGE:
            context.update(
                candidate_proposal=proposal_excerpt,
                candidate_proposal_is_excerpt=bool(proposal),
                full_review_archived=True,
                reason=row["reason"][:400],
                partial_steps=[
                    {
                        "text": step["text"],
                        "quote_indexes": list(
                            dict.fromkeys(
                                index for old_index in step["quote_indexes"] for index in piece_indexes[old_index]
                            )
                        ),
                    }
                    for step in row["partial_steps"]
                ],
            )
        controls["support_units"].append(context)
    controls["allowed_source_refs"] = list(dict.fromkeys(chunk["source_uri"] for chunk in evidence))
    if descriptor.get("source_unit_review_protocol"):
        from .source_unit_review import quote_roles

        controls.update(quote_roles(descriptor, evidence))
    if evidence:
        for metadata_limit in (None, 128, 64, 0):
            if stage != quote_units.QUOTE_STAGE and metadata_limit is not None:
                break
            if metadata_limit is not None:
                for context in controls["support_units"]:
                    context["candidate_proposal"] = (
                        {key: value[:metadata_limit] for key, value in context["candidate_proposal"].items()}
                        if metadata_limit
                        else {}
                    )
                    context["reason"] = context["reason"][:metadata_limit]
                    context["partial_steps"] = (
                        [
                            {"text": step["text"][:metadata_limit], "quote_indexes": step["quote_indexes"]}
                            for step in context["partial_steps"]
                        ]
                        if metadata_limit
                        else []
                    )
                    context["partial_steps_are_excerpts"] = True
                    context["provisional_context_may_be_omitted"] = True
            try:
                planning.build_stage_packet(
                    stage,
                    "evidence-review.json",
                    evidence_chunks=evidence,
                    controls=controls,
                    language_policy=policy,
                    max_chars=max_chars,
                )
                break
            except ValueError as error:
                if "bounded input budget" not in str(error) or stage != quote_units.QUOTE_STAGE or metadata_limit == 0:
                    raise
    return evidence, controls, cursor + len(units), [unit["id"] for unit in units]
