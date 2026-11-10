"""Assemble only explicit memberships, with deterministic replay-stable IDs."""

from .input import canonical, sha


def assemble(window, receipt):
    members = {row["member_ref"]: row for row in window["members"]}
    dimensions, links = [], []
    for number, group in enumerate(receipt["groups"], 1):
        identity = [window["window_id"], number, group["member_refs"]]
        dimension_id = "dim_" + sha(canonical(identity))[:32]
        selected = [members[ref] for ref in group["member_refs"]]
        case_count = len({m["case_ref"] for m in selected})
        dimensions.append({"dimension_id": dimension_id, "name": group.get("name") or group["definition"],
                           "definition": group["definition"],
                           "observable_behaviors": group.get("observable_behaviors", []),
                           "applicability": group.get("applicability", {}),
                           "evidence_scope": window["partition"], "support_case_count": case_count,
                           "link_page_refs": [f"case_dimension_links/{window['window_id']}.jsonl"],
                           "notes": [*group.get("notes", []), *([] if case_count > 1 else ["single_support_case"])]})
        for member in selected:
            links.append({"dimension_id": dimension_id, "observation_ref": member["member_ref"],
                          "case_ref": member["case_ref"], "case_version": member["case_version"],
                          "sentence_refs": member["sentence_refs"], "source_spans": member["source_spans"]})
    return dimensions, links


def dispositions(window, receipt):
    """Retain method-level findings and their source without inventing capabilities."""
    members = {row["member_ref"]: row for row in window["members"]}
    return [
        {**row, **members[row["member_ref"]], "reason": row.get("reason", ""),
         "window_id": window["window_id"], "disposition": kind}
        for kind in ("kept_separate", "deferred")
        for row in receipt[kind]
    ]
