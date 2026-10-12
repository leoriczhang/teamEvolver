"""Deterministic human view of the validated canonical Test Dataset."""

import json
from typing import Any


def dataset_markdown(document: dict[str, Any]) -> str:
    """Keep case identity, query, Checklist and provenance identical to JSON."""
    lines = [f"# {document['name']}", "", str(document.get("description") or ""), ""]
    for case in document["cases"]:
        lines.extend([f"## {case['case_id']}", "", case["query"], "", "### Checklist", ""])
        for check in case["checks"]:
            polarity = " · must_not" if check.get("polarity") == "must_not" else ""
            lines.append(f"- [{check['id']} · {check['kind']}{polarity}] {check['text']}")
        lines.extend(["", "### Evidence", ""])
        lines.extend(f"- `{uri}`" for uri in case["provenance"]["session_ids"])
        if case.get("materials"):
            lines.extend(
                ["", "### Materials", "", "```json", json.dumps(case["materials"], ensure_ascii=False, indent=2), "```"]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
