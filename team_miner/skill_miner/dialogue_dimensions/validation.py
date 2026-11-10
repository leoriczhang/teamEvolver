"""Permissive envelopes, exact provenance, and local issue isolation."""

import json
import re

from .input import canonical, sha


class ReceiptError(ValueError):
    """The unit needs compensation; unrelated accepted receipts remain usable."""


def decode(text):
    repairs = []
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S)
    if match:
        text = match[1]
        repairs.append("unwrapped_code_fence")
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ReceiptError("unrecoverable_json") from exc
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
        repairs.append("unwrapped_singleton")
    if not isinstance(value, dict):
        raise ReceiptError("receipt_must_be_object")
    return value, repairs


def sequence(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def refs(value, index):
    values = value if isinstance(value, list) else [value] if isinstance(value, (str, int)) else []
    good = []
    for item in values:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            continue
        if isinstance(item, int) and not isinstance(item, bool):
            item = f"s{item:06d}"
        if item in index and item not in good:
            good.append(item)
    return good


def quote_check(raw, case, index):
    result, issues = [], []
    for quote in sequence(raw):
        if not isinstance(quote, dict):
            issues.append({"reason": "invalid_quote_object", "submitted": quote})
            continue
        item = dict(quote)
        if (item.get("case_ref", case["case_ref"]) != case["case_ref"]
                or item.get("case_version", case["case_version"]) != case["case_version"]):
            issues.append({"reason": "foreign_quote_source", "submitted": item})
            continue
        selected = refs(item.get("sentence_ids"), index)
        text = item.get("text")
        if not isinstance(text, str):
            issues.append({"reason": "missing_quote_text", "submitted": item})
            continue
        if not selected:
            start = case["dialogue_text"].find(text)
            if start >= 0 and case["dialogue_text"].find(text, start + 1) < 0:
                selected = [key for key, span in index.items()
                            if span["char_start"] <= start and span["char_end"] >= start + len(text)]
                if not selected:
                    selected = [key for key, span in index.items()
                                if start <= span["char_start"] and span["char_end"] <= start + len(text)]
        if not selected:
            issues.append({"reason": "unlocatable_quote", "submitted": item})
            continue
        ordered = sorted(selected, key=lambda key: index[key]["char_start"])
        start, end = index[ordered[0]]["char_start"], index[ordered[-1]]["char_end"]
        expected = case["dialogue_text"][start:end]
        if any(start < span["char_start"] < end and key not in ordered for key, span in index.items()):
            issues.append({"reason": "non_contiguous_quote", "submitted": item})
            continue
        audit = "exact"
        if text != expected:
            # Keep altered text in issues. A submitted exact substring may be expanded separately.
            audit = "expanded_to_sentence" if text in expected else "host_restored_from_sentence_refs"
            issues.append({"reason": "quote_not_full_exact_sentence", "submitted": item, "recovery": audit})
        elif item.get("char_start", start) != start or item.get("char_end", end) != end:
            audit = "relocated_exact_quote"
        result.append({"case_ref": case["case_ref"], "case_version": case["case_version"],
                       "sentence_ids": ordered, "char_start": start, "char_end": end,
                       "text": expected, "audit": audit})
    return result, issues


def findings(text, case):
    raw, repairs = decode(text)
    for key, expected in (("schema_version", "taikang.dialogue-findings.v1"),
                          ("case_ref", case["case_ref"]), ("case_version", case["case_version"])):
        if key not in raw:
            raw[key] = expected
            repairs.append("filled_" + key)
        elif raw[key] != expected:
            raise ReceiptError("foreign_" + key)
    if raw.get("analysis_status") not in {"analyzed", "analyzed_with_issues"}:
        raise ReceiptError("missing_real_analysis_receipt")
    if "observations" not in raw:
        raise ReceiptError("missing_observations")
    observations = raw["observations"]
    if isinstance(observations, dict):
        observations = [observations]
        repairs.append("observation_object_to_array")
    if not isinstance(observations, list):
        raise ReceiptError("invalid_observations")
    index = {row["sentence_id"]: row for row in case["sentence_index"]}
    usable, isolated, issues, seen = [], [], [], set()
    for number, submitted in enumerate(observations, 1):
        if not isinstance(submitted, dict):
            isolated.append({"submitted": submitted, "reason": "empty_behavior"})
            continue
        item = dict(submitted)
        behavior = item.get("behavior")
        if not isinstance(behavior, str) or not behavior.strip():
            isolated.append({"submitted": submitted, "reason": "empty_behavior"})
            continue
        local = str(item.get("local_ref") or f"o{number}")
        if local in seen:
            local = f"{local}-{number}"
            repairs.append("duplicate_local_ref")
        seen.add(local)
        selected = refs(item.get("sentence_refs"), index)
        quotes, quote_issues = quote_check(item.get("quote"), case, index)
        issues.extend({"local_ref": local, **issue} for issue in quote_issues)
        if not selected and quotes:
            selected = list(dict.fromkeys(key for q in quotes for key in q["sentence_ids"]))
            repairs.append("sentence_refs_from_exact_quote")
        item.update(local_ref=local, label=item.get("label") or behavior,
                    observation_ref="obs_" + sha(canonical([case["case_ref"], case["case_version"],
                                                          sha(text), local]))[:32],
                    case_ref=case["case_ref"], case_version=case["case_version"], sentence_refs=selected,
                    customer_response_refs=refs(item.get("customer_response_refs"), index),
                    applicability=item.get("applicability") if isinstance(item.get("applicability"), dict) else {},
                    notes=sequence(item.get("notes")), display_quotes=quotes)
        item.pop("quote", None)
        item["source_spans"] = [index[key] for key in selected]
        if not selected:
            isolated.append({"submitted": submitted, "reason": "unlocatable_observation", "needs_review": True})
            continue
        if quote_issues:
            item["needs_review"] = True
        usable.append(item)
    if not observations and not raw.get("empty_reason"):
        issues.append({"reason": "missing_optional_empty_reason"})
    return {"schema_version": raw["schema_version"], "case_ref": case["case_ref"],
            "case_version": case["case_version"], "analysis_status": "analyzed_with_issues"
            if issues or isolated else "analyzed", "observations": usable, "isolated": isolated,
            "issues": issues, "repairs": repairs, "empty_reason": raw.get("empty_reason", ""),
            "notes": sequence(raw.get("notes"))}


def merge(text, window):
    raw, repairs = decode(text)
    if raw.get("window_id", window["window_id"]) != window["window_id"]:
        raise ReceiptError("foreign_window_id")
    if raw.get("schema_version", "taikang.dimension-merge.v1") != "taikang.dimension-merge.v1":
        raise ReceiptError("foreign_merge_schema")
    known = {row["member_ref"] for row in window["members"]}
    covered, groups, separate, deferred = set(), [], [], []
    for group in sequence(raw.get("groups")):
        if not isinstance(group, dict):
            raise ReceiptError("invalid_group_object")
        members = group.get("member_refs", [])
        if isinstance(members, str):
            members = [members]
            repairs.append("member_ref_string_to_array")
        if (not isinstance(members, list) or not members
                or any(not isinstance(m, str) or m not in known for m in members)):
            raise ReceiptError("foreign_or_empty_group_members")
        if group.get("existing_dimension_id"):
            allowed = {d["dimension_id"] for d in window.get("existing_dimensions", [])}
            if group["existing_dimension_id"] not in allowed:
                raise ReceiptError("foreign_existing_dimension")
        if not isinstance(group.get("definition"), str) or not group["definition"].strip():
            raise ReceiptError("missing_group_definition")
        groups.append({**group, "member_refs": list(dict.fromkeys(members)),
                       "notes": sequence(group.get("notes")),
                       "applicability": group.get("applicability")
                       if isinstance(group.get("applicability"), dict) else {},
                       "observable_behaviors": group.get("observable_behaviors")
                       if isinstance(group.get("observable_behaviors"), list) else []})
        covered.update(members)
    for key, target in (("kept_separate", separate), ("deferred", deferred)):
        for row in sequence(raw.get(key)):
            if not isinstance(row, dict):
                raise ReceiptError("invalid_disposition_object")
            ref = row.get("member_ref")
            if not isinstance(ref, str) or ref not in known:
                raise ReceiptError("foreign_disposition_member")
            covered.add(ref)
            target.append(row)
    if known - covered:
        raise ReceiptError("missing_member_disposition")
    return {"window_id": window["window_id"], "groups": groups, "kept_separate": separate,
            "deferred": deferred, "notes": sequence(raw.get("notes")), "repairs": repairs}
