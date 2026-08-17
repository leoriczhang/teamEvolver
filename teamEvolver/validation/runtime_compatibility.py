"""Cross-runtime validation policy for portable team Skills."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from ..integrations.agent_protocol import (
    CAP_REPLAY_BRANCH,
    CAP_SKILL_BUNDLE,
)


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


def _active_replay_agents(
    agents: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for agent in agents:
        if str(agent.get("status") or "active") != "active":
            continue
        capabilities = set(agent.get("capability_ids") or [])
        legacy = set(agent.get("capabilities") or [])
        if CAP_REPLAY_BRANCH not in capabilities and "true_replay" not in legacy:
            continue
        runtime = _runtime(
            agent.get("runtime_class") or agent.get("runtime_type")
        )
        if not runtime:
            continue
        current = selected.get(runtime)
        if current is None or str(agent.get("updated_at") or "") > str(
            current.get("updated_at") or ""
        ):
            selected[runtime] = dict(agent)
    return selected


def _agent_compatibility_error(
    agent: dict[str, Any],
    declaration: dict[str, Any],
) -> str:
    capabilities = {
        _runtime(item)
        for item in agent.get("capability_ids") or []
        if _runtime(item)
    }
    missing = sorted(
        set(declaration["required_agent_capabilities"]) - capabilities
    )
    if missing:
        return "missing capabilities: " + ", ".join(missing)
    details = (
        agent.get("capability_details")
        if isinstance(agent.get("capability_details"), dict)
        else {}
    )
    replay_detail = (
        details.get(CAP_REPLAY_BRANCH)
        if isinstance(details.get(CAP_REPLAY_BRANCH), dict)
        else {}
    )
    metadata = (
        agent.get("metadata")
        if isinstance(agent.get("metadata"), dict)
        else {}
    )
    tools = set(
        _string_list(replay_detail.get("tools"))
        + _string_list(metadata.get("tools"))
    )
    missing_tools = sorted(set(declaration["required_tools"]) - tools)
    if missing_tools:
        return "missing tools: " + ", ".join(missing_tools)
    platforms = set(_string_list(metadata.get("platforms")))
    if _runtime(metadata.get("platform")):
        platforms.add(_runtime(metadata.get("platform")))
    if (
        declaration["supported_platforms"]
        and not platforms.intersection(declaration["supported_platforms"])
    ):
        return "unsupported platform"
    bundle_format = declaration["bundle_format"]
    bundle_detail = (
        details.get(CAP_SKILL_BUNDLE)
        if isinstance(details.get(CAP_SKILL_BUNDLE), dict)
        else {}
    )
    formats = set(_string_list(bundle_detail.get("formats")))
    if bundle_format and formats and bundle_format not in formats:
        return f"unsupported bundle format: {bundle_format}"
    return ""


def prepare_runtime_validation(
    *,
    skill: dict[str, Any],
    sessions: list[dict[str, Any]],
    replay_cases: list[dict[str, Any]],
    agents: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a validation matrix and neutral cases for missing runtime classes."""
    declaration = _runtime_declaration(skill)
    replay_agents = _active_replay_agents(agents)
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
                    "external_subject",
                )
                if context.get(key) not in (None, "")
            }
        cases.append(case)
    source_runtimes = _source_runtimes(sessions, cases)
    if declaration["supported_runtimes"]:
        required = list(declaration["supported_runtimes"])
    elif declaration["portable"]:
        required = sorted({*replay_agents, *source_runtimes})
    else:
        required = list(source_runtimes)

    missing_capabilities = sorted(
        runtime for runtime in required if runtime not in replay_agents
    )
    incompatible = {
        runtime: reason
        for runtime in required
        if runtime in replay_agents
        and (
            reason := _agent_compatibility_error(
                replay_agents[runtime],
                declaration,
            )
        )
    }
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
            or runtime not in replay_agents
            or runtime in incompatible
            or seed is None
        ):
            continue
        agent = replay_agents[runtime]
        metadata = (
            agent.get("metadata")
            if isinstance(agent.get("metadata"), dict)
            else {}
        )
        digest = hashlib.sha256(
            json.dumps(
                {
                    "runtime": runtime,
                    "agent_id": agent.get("agent_id"),
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
            "source_runtime": {
                "type": runtime,
                "integration_id": str(agent.get("agent_id") or ""),
            },
            "source_runtime_context": {
                key: metadata.get(key)
                for key in (
                    "tenant_id",
                    "profile_id",
                    "environment_id",
                    "model_config_id",
                )
                if metadata.get(key) not in (None, "")
            },
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
        "available_replay_runtimes": sorted(replay_agents),
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
    """Require an independent accepted result for every runtime class."""
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
            if accepted:
                matrix[runtime] = {
                    "status": "accepted",
                    "accepted": True,
                    "decision": decision or "accept",
                }
            elif rejected and current.get("status") != "accepted":
                matrix[runtime] = {
                    "status": "rejected",
                    "accepted": False,
                    "decision": "reject",
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
                "missing replay capability: "
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


def skill_supports_runtime(skill: dict[str, Any], runtime_type: str) -> bool:
    """Return whether one published Skill may be distributed to a runtime."""
    runtime = _runtime(runtime_type)
    declaration = _runtime_declaration(skill)
    policy = (
        skill.get("runtime_policy")
        if isinstance(skill.get("runtime_policy"), dict)
        else {}
    )
    distribution = [
        _runtime(item)
        for item in policy.get("distribution_runtimes") or []
        if _runtime(item)
    ]
    if distribution:
        return runtime in distribution
    if declaration["supported_runtimes"]:
        return runtime in declaration["supported_runtimes"]
    return bool(declaration["portable"] or declaration["declaration"] == "legacy_source_only")


def reconcile_open_validation_jobs(
    config: Any,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Migrate replayable legacy jobs and supersede identity-less jobs."""
    from ..integrations.agent_registry import list_agents
    from ..session_store import SessionStore
    from .store import ValidationStore

    validation_store = ValidationStore.from_config(config)
    session_store = SessionStore.from_config(config)
    actions: list[dict[str, Any]] = []
    for job in validation_store.list_open_jobs():
        if isinstance(job.get("runtime_validation_policy"), dict):
            continue
        job_id = str(job.get("job_id") or "")
        cases = [
            dict(case)
            for case in job.get("replay_cases") or []
            if isinstance(case, dict)
        ]
        sessions: list[dict[str, Any]] = []
        identity_errors: list[str] = []
        seen_sessions: set[str] = set()
        for case in cases:
            session_id = str(case.get("session_id") or "")
            session = (
                session_store.load_session(session_id)
                if session_id
                else None
            )
            if not isinstance(session, dict):
                identity_errors.append(
                    f"case {session_id or '<unknown>'}: source session missing"
                )
                continue
            if session_id not in seen_sessions:
                sessions.append(session)
                seen_sessions.add(session_id)
            runtime = (
                session.get("runtime")
                if isinstance(session.get("runtime"), dict)
                else {}
            )
            context = (
                session.get("runtime_context")
                if isinstance(session.get("runtime_context"), dict)
                else {}
            )
            runtime_type = _runtime(
                runtime.get("type") or session.get("source")
            )
            if runtime_type == "agentshub" and not str(
                context.get("tenant_id") or ""
            ):
                identity_errors.append(
                    f"case {session_id}: AgentsHub tenant identity missing"
                )
            elif runtime_type in {"cli", "hermes", "hermes-cli"} and not str(
                runtime.get("integration_id")
                or context.get("profile_id")
                or ""
            ):
                identity_errors.append(
                    f"case {session_id}: Hermes profile identity missing"
                )
            elif not runtime_type:
                identity_errors.append(
                    f"case {session_id}: runtime identity missing"
                )
            if context.get("candidate_job_id"):
                identity_errors.append(
                    f"case {session_id}: candidate-audit source is forbidden"
                )

        if identity_errors:
            action = {
                "job_id": job_id,
                "action": "supersede",
                "reasons": sorted(set(identity_errors)),
            }
            actions.append(action)
            if not dry_run:
                validation_store.save_decision(
                    job_id,
                    {
                        "status": "superseded",
                        "reason": (
                            "legacy replay case failed runtime identity "
                            "preflight"
                        ),
                        "preflight_errors": action["reasons"],
                    },
                )
            continue

        candidate = (
            dict(job.get("candidate_skill"))
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        prepared = prepare_runtime_validation(
            skill=candidate,
            sessions=sessions,
            replay_cases=cases,
            agents=list_agents(config),
        )
        action = {
            "job_id": job_id,
            "action": "migrate",
            "required_runtimes": prepared["policy"][
                "required_runtimes"
            ],
            "new_revision": max(
                1,
                int(job.get("candidate_revision") or 1) + 1,
            ),
        }
        actions.append(action)
        if dry_run:
            continue
        updated = {
            **job,
            "candidate_revision": action["new_revision"],
            "candidate_skill": {
                **candidate,
                "runtime_policy": prepared["policy"],
            },
            "replay_cases": prepared["replay_cases"],
            "runtime_validation_policy": prepared["policy"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "migration": {
                "kind": "runtime-validation-v1",
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        validation_store.reset_job_artifacts(job_id)
        validation_store.save_job(updated)
    return {
        "dry_run": dry_run,
        "migrated": sum(item["action"] == "migrate" for item in actions),
        "superseded": sum(
            item["action"] == "supersede" for item in actions
        ),
        "actions": actions,
    }
