"""Replay Runtime capability matrix: preparation and result adjudication."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable



def _runtime(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _metadata(skill: dict[str, Any]) -> dict[str, Any]:
    extra = (
        skill.get("extra_frontmatter")
        if isinstance(skill.get("extra_frontmatter"), dict)
        else skill.get("_extra_frontmatter")
        if isinstance(skill.get("_extra_frontmatter"), dict)
        else {}
    )
    policy = (
        skill.get("runtime_policy")
        if isinstance(skill.get("runtime_policy"), dict)
        else {}
    )
    return {**extra, **policy, **skill}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return sorted(
        {
            _runtime(item)
            for item in value or []
            if _runtime(item)
        }
    )


def _runtime_declaration(skill: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(skill)
    supported = _string_list(metadata.get("supported_runtimes"))
    portable_value = metadata.get("portable")
    portable = (
        portable_value
        if isinstance(portable_value, bool)
        else str(portable_value or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    return {
        "portable": bool(portable),
        "supported_runtimes": supported,
        "required_agent_capabilities": _string_list(
            metadata.get("required_agent_capabilities")
        ),
        "required_tools": _string_list(metadata.get("required_tools")),
        "supported_platforms": _string_list(
            metadata.get("supported_platforms")
        ),
        "bundle_format": _runtime(
            metadata.get("bundle_format")
            or metadata.get("format")
        ),
        "declaration": (
            "supported_runtimes"
            if supported
            else "portable"
            if portable
            else "legacy_source_only"
        ),
    }


def runtime_type_for_case(case: dict[str, Any]) -> str:
    runtime = (
        case.get("source_runtime")
        if isinstance(case.get("source_runtime"), dict)
        else {}
    )
    return _runtime(
        runtime.get("type")
        or case.get("runtime_type")
        or case.get("source")
    )


def _source_runtimes(
    sessions: Iterable[dict[str, Any]],
    cases: Iterable[dict[str, Any]],
) -> list[str]:
    values: set[str] = set()
    for session in sessions:
        runtime = (
            session.get("runtime")
            if isinstance(session.get("runtime"), dict)
            else {}
        )
        value = _runtime(runtime.get("type") or session.get("source"))
        if value:
            values.add(value)
    for case in cases:
        value = runtime_type_for_case(case)
        if value:
            values.add(value)
    return sorted(values)


def prepare_runtime_validation(
    *,
    skill: dict[str, Any],
    sessions: list[dict[str, Any]],
    replay_cases: list[dict[str, Any]],
    validation_runtimes: list[str],
) -> dict[str, Any]:
    """Build a validation matrix and neutral cases for missing runtime classes."""
    declaration = _runtime_declaration(skill)
    configured_runtimes = set(_string_list(validation_runtimes))
    sessions_by_id = {
        str(session.get("session_id") or ""): session
        for session in sessions
        if str(session.get("session_id") or "")
    }
    cases: list[dict[str, Any]] = []
    for raw_case in replay_cases:
        case = dict(raw_case)
        source = sessions_by_id.get(str(case.get("session_id") or ""))
        if source and not isinstance(case.get("source_runtime"), dict):
            runtime = (
                source.get("runtime")
                if isinstance(source.get("runtime"), dict)
                else {}
            )
            case["source_runtime"] = dict(runtime)
        if source and not isinstance(
            case.get("source_runtime_context"),
            dict,
        ):
            context = (
                source.get("runtime_context")
                if isinstance(source.get("runtime_context"), dict)
                else {}
            )
            case["source_runtime_context"] = {
                key: context.get(key)
                for key in (
                    "tenant_id",
                    "profile_id",
                    "environment_id",
                    "model_config_id",
                    "user_id",
                )
                if context.get(key) not in (None, "")
            }
        cases.append(case)
    source_runtimes = _source_runtimes(sessions, cases)
    if declaration["supported_runtimes"]:
        required = list(declaration["supported_runtimes"])
    elif declaration["portable"]:
        required = sorted({*configured_runtimes, *source_runtimes})
    else:
        required = list(source_runtimes)

    missing_capabilities = sorted(
        runtime for runtime in required if runtime not in configured_runtimes
    )
    incompatible = {}
    represented = {
        runtime_type_for_case(case)
        for case in cases
        if runtime_type_for_case(case)
    }
    seed = next(
        (
            dict(case)
            for case in cases
            if str(case.get("instruction") or case.get("query") or "").strip()
        ),
        None,
    )
    for runtime in required:
        if (
            runtime in represented
            or runtime not in configured_runtimes
            or runtime in incompatible
            or seed is None
        ):
            continue
        digest = hashlib.sha256(
            json.dumps(
                {
                    "runtime": runtime,
                    "case": seed.get("case_id") or seed.get("dataset_id"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:20]
        neutral = {
            **seed,
            "case_id": f"compat-{runtime}-{digest}",
            "dataset_id": f"compat-{runtime}-{digest}",
            "session_id": f"compat-{runtime}-{digest}",
            "source_session_ids": [],
            "source_runtime": {"type": runtime},
            "source_runtime_context": {},
            "context_snapshot": {},
            "context_snapshot_id": "",
            "context_policy": {
                "mode": "neutral",
                "team_only": True,
                "personal_context": False,
            },
            "evidence_window": "compatibility",
            "compatibility_runtime": runtime,
        }
        cases.append(neutral)
        represented.add(runtime)

    policy = {
        **declaration,
        "source_runtimes": source_runtimes,
        "required_runtimes": required,
        "available_replay_runtimes": sorted(configured_runtimes),
        "missing_replay_runtimes": missing_capabilities,
        "incompatible_runtimes": incompatible,
        "distribution_runtimes": (
            required
            if declaration["supported_runtimes"] or declaration["portable"]
            else []
        ),
    }
    return {"policy": policy, "replay_cases": cases}


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
            "status": "passed",
            "required_runtimes": [],
            "matrix": {},
            "reason": "legacy job has no runtime matrix",
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
            if not runtime and len(required) == 1:
                runtime = required[0]
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
            accepted = bool(raw_entry.get("accepted")) or decision == "accept"
            rejected = bool(raw_entry.get("rejected")) or decision == "reject"
            current = matrix[runtime]
            if rejected:
                matrix[runtime] = {
                    "status": "rejected",
                    "accepted": False,
                    "decision": "reject",
                }
            elif accepted and current.get("status") != "rejected":
                matrix[runtime] = {
                    "status": "accepted",
                    "accepted": True,
                    "decision": decision or "accept",
                }
            elif current.get("status") == "missing":
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
    return {
        "status": status,
        "required_runtimes": required,
        "matrix": matrix,
        "reason": reason,
    }
