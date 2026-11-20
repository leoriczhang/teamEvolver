"""Completed Candidate evidence remains independent of A/B efficiency."""


def candidate_complete(result):
    pairs = result.get("cases") or []
    return bool(pairs) and all(
        pair.get("candidate", {}).get("completed") is True
        and pair.get("candidate", {}).get("ok") is True
        and (pair.get("candidate", {}).get("checklist_report") or {}).get("judge") != "unavailable"
        and (pair.get("candidate", {}).get("checklist_report") or {}).get("all_satisfied") is True
        for pair in pairs
    )
