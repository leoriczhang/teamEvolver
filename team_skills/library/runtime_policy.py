"""Skill-side runtime distribution policy and legacy candidate reconciliation.

Split from the former validation.runtime_compatibility: the Replay Runtime
capability matrix now lives in team_replay.runtime_matrix; this module keeps the
Skill-domain concerns (distribution eligibility, open-job reconciliation).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from team_replay.runtime_matrix import (
    _runtime,
    _runtime_declaration,
    prepare_runtime_validation,
)


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
    from teamEvolver.session_store import SessionStore
    from team_skills.candidates.store import ValidationStore

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
            if not runtime_type:
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
            validation_runtimes=list(config.validation_runtimes),
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
