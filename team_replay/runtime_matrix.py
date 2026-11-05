"""Replay Runtime capability matrix: preparation and result adjudication."""

from __future__ import annotations

from typing import Any


def _runtime(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def runtime_type_for_case(case: dict[str, Any]) -> str:
    provenance = (
        case.get("provenance")
        if isinstance(case.get("provenance"), dict)
        else {}
    )
    runtime = (
        provenance.get("source_runtime")
        if isinstance(provenance.get("source_runtime"), dict)
        else {}
    )
    return _runtime(runtime.get("type"))


def evaluate_runtime_compatibility(
    policy: dict[str, Any] | None,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require acceptance without explicit rejection for every runtime class."""
    policy = dict(policy or {})
    required = sorted(
        {_runtime(item) for item in policy.get("required_runtimes") or [] if _runtime(item)}
    )
    if not required:
        return {
            "status": "blocked",
            "required_runtimes": [],
            "matrix": {},
            "reason": "runtime validation matrix is required",
        }

    matrix: dict[str, dict[str, Any]] = {
        runtime: {"status": "missing", "accepted": False}
        for runtime in required
    }
    for result in results:
        per_runtime = (
            result.get("runtime_validation")
            if isinstance(result.get("runtime_validation"), dict)
            else {}
        )
        if per_runtime:
            entries = per_runtime.items()
        else:
            runtime = _runtime(result.get("runtime_type"))
            entries = [(runtime, result)] if runtime else []
        for raw_runtime, raw_entry in entries:
            runtime = _runtime(raw_runtime)
            if runtime not in matrix or not isinstance(raw_entry, dict):
                continue
            decision = str(
                raw_entry.get("decision")
                or raw_entry.get("verdict")
                or ""
            ).lower()
            accepted = decision == "accept" and raw_entry.get("accepted") is True
            if raw_entry.get("status") not in {"evaluated", "completed"}:
                accepted = False
            rejected = bool(raw_entry.get("rejected")) or decision == "reject"
            current = matrix[runtime]
            if rejected:
                matrix[runtime] = {
                    "status": "rejected",
                    "accepted": False,
                    "decision": "reject",
                }
            elif accepted and current.get("status") not in {"rejected", "inconclusive"}:
                matrix[runtime] = {
                    "status": "accepted",
                    "accepted": True,
                    "decision": decision or "accept",
                }
            elif not accepted and current.get("status") != "rejected":
                matrix[runtime] = {
                    "status": "inconclusive",
                    "accepted": False,
                    "decision": decision or "inconclusive",
                }

    missing_capabilities = sorted(
        {
            _runtime(item)
            for item in policy.get("missing_replay_runtimes") or []
            if _runtime(item)
        }
    )
    incompatible = {
        _runtime(runtime): str(reason)
        for runtime, reason in (
            policy.get("incompatible_runtimes") or {}
        ).items()
        if _runtime(runtime)
    }
    statuses = {runtime: entry["status"] for runtime, entry in matrix.items()}
    targets = policy.get("required_targets") or []
    target_matrix = {}
    for target in targets:
        ident = target["connection_id"] if isinstance(target, dict) else str(target)
        entries = [(r.get("target_validation") or {}).get(ident) for r in results]
        entries = [e for e in entries if isinstance(e, dict)]
        if isinstance(target, dict):
            entries = [e for e in entries if all(e.get(k) == target[k] for k in
                       ("connection_revision", "config_hash", "binding_hash", "runtime_hash") if k in target)]
        if not entries:
            target_matrix[ident] = {"status": "missing", "accepted": False}
        elif any(e.get("verdict") == "reject" for e in entries):
            target_matrix[ident] = {"status": "rejected", "accepted": False}
        elif all(e.get("accepted") is True and e.get("status") in {"evaluated", "completed"} for e in entries):
            target_matrix[ident] = {"status": "accepted", "accepted": True}
        else:
            target_matrix[ident] = {"status": "inconclusive", "accepted": False}
        if isinstance(target, dict):
            target_matrix[ident].update({k: target[k] for k in
                                        ("connection_revision", "config_hash", "binding_hash", "runtime_hash")
                                        if k in target})
    if missing_capabilities or incompatible:
        status = "blocked"
        parts = []
        if missing_capabilities:
            parts.append(
                "runtime not configured for validation: "
                + ", ".join(missing_capabilities)
            )
        if incompatible:
            parts.append(
                "incompatible runtimes: "
                + ", ".join(
                    f"{runtime} ({reason})"
                    for runtime, reason in sorted(incompatible.items())
                )
            )
        reason = "; ".join(parts)
    elif any(value == "rejected" for value in statuses.values()):
        status = "rejected"
        reason = "one or more runtime classes rejected the candidate"
    elif all(value == "accepted" for value in statuses.values()):
        status = "passed"
        reason = "every required runtime class accepted independently"
    elif any(value == "inconclusive" for value in statuses.values()):
        status = "inconclusive"
        reason = "one or more runtime classes are inconclusive"
    else:
        status = "pending"
        reason = "runtime validation results are incomplete"
    if target_matrix and any(not t["accepted"] for t in target_matrix.values()):
        status = "rejected" if any(t["status"] == "rejected" for t in target_matrix.values()) else "blocked"
        reason = "one or more managed MA targets lack independent acceptance"
    return {
        "status": status,
        "required_runtimes": required,
        "matrix": matrix,
        "reason": reason,
        "required_targets": targets, "target_matrix": target_matrix,
    }
