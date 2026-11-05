"""Deterministic human view of the validated canonical Test Dataset."""

import json
from typing import Any


def dataset_markdown(document: dict[str, Any]) -> str:
    """Keep case identity, query, Checklist and provenance identical to JSON."""
    chinese = any("\u3400" <= char <= "\u9fff" for char in str(document["name"]))
    lines = [f"# {document['name']}", "", str(document.get("description") or ""), ""]
    for index, case in enumerate(document["cases"], 1):
        title = f"案例 {index}" if chinese else f"Case {index}"
        lines.extend([f"## {title}", "", f"`{case['case_id']}`", "", case["query"], "",
                      "### 检查清单（Checklist）" if chinese else "### Checklist", ""])
        for check in case["checks"]:
            polarity = " · must_not" if check.get("polarity") == "must_not" else ""
            lines.append(f"- [{check['id']} · {check['kind']}{polarity}] {check['text']}")
        lines.extend(["", "### 来源依据（Evidence）" if chinese else "### Evidence", ""])
        lines.extend(f"- `{uri}`" for uri in case["provenance"]["session_ids"])
        if case.get("materials"):
            lines.extend(
                ["", "### 案例材料" if chinese else "### Materials", "", "```json",
                 json.dumps(case["materials"], ensure_ascii=False, indent=2), "```"]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
