"""Audited omissions in generated Case queries; original Native outcomes remain immutable."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from . import bounded_compile_plan as planning
from .ov_compile_mining import utc_now

PROTOCOL = "te-case-query-omission.v1"
SCOPE_PROTOCOL = "te-case-scope-omission.v1"


def _host():
    from .ov_staged_orchestration import _json, _read, _save, _sha

    return _json, _read, _save, _sha


def _source_assignment(root: Path, name: str, index: int, value: dict) -> tuple[dict, list[dict]]:
    _, read, _, _ = _host()
    contexts = [row["context"] for p in (root / "case-batches" / name / "outcomes").glob(f"*-{index:02d}.json")
                if (row := read(p)).get("status") == "valid" and row.get("case") == value]
    if len(contexts) != 1:
        raise ValueError("Case correction needs its unique accepted source assignment")
    context = contexts[0]
    chunks = [read(root / "chunks" / f"{read(root / 'chunk-ids' / (identity + '.json'))['ordinal']:08d}.json")
              for identity in context["input_ids"]]
    planning.validate_chunks(chunks)
    if [row["id"] for row in chunks] != context["input_ids"]:
        raise ValueError("Case correction source identities changed")
    planning.validate_case(value, case_id=context["case_id"], skill_name=name, source_refs=context["source_refs"])
    return context, chunks


def _apply(value: dict, record: dict, chunks: list[dict]) -> dict:
    json_text, _, _, sha = _host()
    edit = record["omission"]
    if record.get("protocol") != PROTOCOL or sha(json_text(value)) != record["original_case_sha256"]:
        raise ValueError("Case correction original Case or protocol changed")
    if record["source_fingerprints"] != [[row["id"], row["sha256"]] for row in chunks]:
        raise ValueError("Case correction original source SHA changed")
    start, omitted = edit["start"], edit["text"]
    if type(start) is not int or start < 0 or not isinstance(omitted, str) or not 1 <= len(omitted) <= 160:
        raise ValueError("Case correction requires one bounded exact omission")
    if value["query"][start:start + len(omitted)] != omitted:
        raise ValueError("Case correction query span changed")
    if any(omitted in row["content"] for row in chunks):
        raise ValueError("A source-backed expression cannot be omitted by this unsupported-name correction")
    result = copy.deepcopy(value)
    result["query"] = value["query"][:start] + value["query"][start + len(omitted):]
    if sha(json_text(result)) != record["corrected_case_sha256"]:
        raise ValueError("Case correction projected Case changed")
    return result


def corrected_case(root: Path, name: str, index: int) -> dict:
    """Render the current reviewed projection without rewriting the accepted Case or its raw audit."""
    _, read, _, _ = _host()
    value = read(root / "cases" / name / f"{index:02d}.json")
    path = root / "case-corrections" / name / f"{index:02d}.json"
    if not path.is_file():
        return value
    context, chunks = _source_assignment(root, name, index, value)
    record = read(path)
    if record.get("accepted_context") != context:
        raise ValueError("Case correction accepted assignment changed")
    if record.get("protocol") == "te-case-source-revision.v1":
        from .case_revisions import revised_case

        return revised_case(root, name, index, value, context, chunks, record)
    result = (_apply_scope(value, record, context, chunks) if record.get("protocol") == SCOPE_PROTOCOL
              else _apply(value, record, chunks))
    return planning.validate_case(result, case_id=context["case_id"], skill_name=name,
                                  source_refs=context["source_refs"])


def omit_unsupported_query_name(root: Path, name: str, index: int, *, expected_case_sha256: str,
                               start: int, text: str, explanation: str) -> dict:
    """An operator-inspected correction removes a name; it cannot insert an identity or resolve unknown facts."""
    json_text, read, save, sha = _host()
    value = read(root / "cases" / name / f"{index:02d}.json")
    if sha(json_text(value)) != expected_case_sha256 or not explanation.strip():
        raise ValueError("Case correction requires the exact inspected Case and a reason")
    context, chunks = _source_assignment(root, name, index, value)
    path = root / "case-corrections" / name / f"{index:02d}.json"
    if path.exists():
        raise ValueError("Case correction cannot overwrite an earlier correction")
    result = copy.deepcopy(value)
    result["query"] = value["query"][:start] + value["query"][start + len(text):]
    record = {
        "protocol": PROTOCOL, "case_id": value["case_id"], "recorded_at": utc_now(),
        "original_case_sha256": expected_case_sha256, "corrected_case_sha256": sha(json_text(result)),
        "source_fingerprints": [[row["id"], row["sha256"]] for row in chunks],
        "accepted_context": context, "omission": {"start": start, "text": text}, "explanation": explanation,
        "unknown_facts_resolved": False, "business_quality_passed": False,
    }
    result = _apply(value, record, chunks)
    planning.validate_case(result, case_id=context["case_id"], skill_name=name, source_refs=context["source_refs"])
    save(path, record)
    return record


def _omit(text: str, edits: list[dict]) -> str:
    previous = len(text)
    for edit in sorted(edits, key=lambda row: row["start"], reverse=True):
        start, omitted = edit["start"], edit["text"]
        if (type(start) is not int or start < 0 or not isinstance(omitted, str)
                or not 1 <= len(omitted) <= 400 or start + len(omitted) > previous
                or text[start:start + len(omitted)] != omitted):
            raise ValueError("Scope correction requires bounded, disjoint exact omissions")
        text = text[:start] + text[start + len(omitted):]
        previous = start
    return text


def _apply_scope(value: dict, record: dict, context: dict, chunks: list[dict]) -> dict:
    json_text, _, _, sha = _host()
    if record.get("protocol") != SCOPE_PROTOCOL or sha(json_text(value)) != record["original_case_sha256"]:
        raise ValueError("Scope correction original Case or protocol changed")
    if record["source_fingerprints"] != [[row["id"], row["sha256"]] for row in chunks]:
        raise ValueError("Scope correction source SHA changed")
    claim = record["foreign_context_claim"]
    method_ids, context_ids = context.get("method_input_ids", []), context.get("context_input_ids", [])
    if (claim["input_id"] not in context_ids or claim["input_id"] in method_ids
            or not claim["quote"] or len(claim["quote"]) > 400
            or not any(row["id"] == claim["input_id"] and claim["quote"] in row["content"] for row in chunks)
            or any(row["id"] in method_ids and claim["quote"] in row["content"] for row in chunks)):
        raise ValueError("Scope correction requires the inspected foreign context claim, not method Evidence")
    result = _project_scope(value, record)
    if sha(json_text(result)) != record["corrected_case_sha256"]:
        raise ValueError("Scope correction projected Case changed")
    return result


def _project_scope(value: dict, record: dict) -> dict:
    result = copy.deepcopy(value)
    edits = record["query_omissions"]
    if not 1 <= len(edits) <= 8:
        raise ValueError("Scope correction needs bounded query omissions")
    result["query"] = _omit(value["query"], edits)
    # After removing a numbered task, compact its display numbering without inventing any task or fact.
    counter = iter(range(1, 100))
    result["query"] = re.sub(r"(?m)^\d+\. ", lambda _: f"{next(counter)}. ", result["query"])
    omitted = record["omitted_checks"]
    check_ids = [row["id"] for row in omitted]
    if not 1 <= len(omitted) <= 4 or len(set(check_ids)) != len(check_ids):
        raise ValueError("Scope correction requires bounded exact checks")
    for row in omitted:
        if [check for check in value["checks"] if check["id"] == row["id"]] != [row]:
            raise ValueError("Scope correction original check changed")
    result["checks"] = [row for row in result["checks"] if row["id"] not in check_ids]
    if len(record["check_text_omissions"]) > 4:
        raise ValueError("Scope correction check text omissions must be bounded")
    for edit in record["check_text_omissions"]:
        matches = [row for row in result["checks"] if row["id"] == edit["id"]]
        if len(matches) != 1:
            raise ValueError("Scope correction check text identity changed")
        matches[0]["text"] = _omit(matches[0]["text"], edit["omissions"])
    return result


def omit_foreign_case_scope(root: Path, name: str, index: int, *, expected_case_sha256: str,
                            foreign_context_claim: dict, query_omissions: list[dict], omitted_checks: list[dict],
                            check_text_omissions: list[dict], explanation: str) -> dict:
    """Record an inspected scope pruning; no replacement scenario, fact or check can be added."""
    json_text, read, save, sha = _host()
    value = read(root / "cases" / name / f"{index:02d}.json")
    if sha(json_text(value)) != expected_case_sha256 or not explanation.strip():
        raise ValueError("Scope correction needs the exact inspected Case and explanation")
    path = root / "case-corrections" / name / f"{index:02d}.json"
    if path.exists():
        raise ValueError("Scope correction cannot overwrite an earlier correction")
    context, chunks = _source_assignment(root, name, index, value)
    record = {
        "protocol": SCOPE_PROTOCOL, "case_id": value["case_id"], "recorded_at": utc_now(),
        "original_case_sha256": expected_case_sha256,
        "source_fingerprints": [[row["id"], row["sha256"]] for row in chunks], "accepted_context": context,
        "foreign_context_claim": foreign_context_claim, "query_omissions": query_omissions,
        "omitted_checks": omitted_checks, "check_text_omissions": check_text_omissions,
        "explanation": explanation, "unknown_facts_resolved": False, "business_quality_passed": False,
    }
    # Compute the expected projection once, then use the same fail-closed verifier on every export.
    projected = _project_scope(value, record)
    record["corrected_case_sha256"] = sha(json_text(projected))
    projected = _apply_scope(value, record, context, chunks)
    planning.validate_case(projected, case_id=context["case_id"], skill_name=name, source_refs=context["source_refs"])
    save(path, record)
    return record
