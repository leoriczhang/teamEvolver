"""Candidate read models and exact version provenance from PostgreSQL."""
from __future__ import annotations

from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.kernel.helpers import parse_skill_content
from team_skills.library.bundle import diff_skill_bundles, encode_bundle_payload
from team_skills.library.repository import SkillLibraryRepository


def skill_payload(name: str, bundle: dict) -> dict | None:
    if not bundle:
        return None
    return {
        **parse_skill_content(name, bundle["SKILL.md"].decode("utf-8")),
        "bundle": encode_bundle_payload(bundle),
    }


def feedback(item: dict, events: list[dict]) -> dict:
    history = []
    for event in events:
        changes = dict(event["detail"]) if event["kind"] == "feedback" else {}
        if event["to_status"] == "published":
            changes.update(adopted=True, rejected=False)
        elif event["to_status"] == "rejected":
            changes.update(adopted=False, rejected=True)
        for field in ("reviewed", "adopted", "rejected"):
            if field in changes:
                history.append({
                    "field": field, "value": changes[field], "actor_id": event["actor"],
                    "actor_name": event["actor"], "at": event["created_at"].isoformat(),
                    "candidate_revision": event["revision"], "version": event["seq"],
                })
    return {
        **{key: item[key] for key in ("reviewed", "adopted", "rejected")},
        "version": events[-1]["seq"] if events else 0,
        "updated_at": item["updated_at"].isoformat(), "history": history,
    }


async def candidate_payload(conn, tx, item: dict, *, compact: bool = False) -> dict:
    repo = CandidateRepository(tx)
    events = await repo.events(conn, item["candidate_id"])
    detail = item["detail"]
    payload = {
        **detail, "job_id": item["candidate_id"], "skill_name": item["skill_name"],
        "candidate_skill_name": item["skill_name"], "proposed_action": item["proposed_action"],
        "candidate_revision": item["revision"], "review_status": item["status"],
        "replay_verdict": item["verdict"], "recommended_publish": item["verdict"] == "accept",
        "feedback": feedback(item, events), "evaluation": item["evaluation"],
        "decision": item["decision"], "validation_job_id": item["validate_task_id"],
        "created_at": item["created_at"].isoformat(), "updated_at": item["updated_at"].isoformat(),
        "decided_at": item["decided_at"].isoformat() if item["decided_at"] else "",
    }
    for key in ("session_evidence", "replay_cases", "evolution_context"):
        payload.pop(key, None)
    if item["status"] == "validating":
        from team_skills.evolution.repository import EvolutionRepository
        command = await EvolutionRepository(tx).command(conn, item["validate_task_id"])
        frozen = command["payload"].get("detail", detail) if command else detail
        payload["replay_progress"] = {
            "total": len(frozen.get("replay_cases") or []),
            **await ReplayRepository(tx).progress(conn, "skill", item["validate_task_id"]),
        }
        payload["replay_sampling"] = frozen.get("replay_sampling") or {}
    if not compact:
        bundle = await repo.read_bundle(conn, item["candidate_id"])
        baseline = {}
        if item["base_version"]:
            baseline = await SkillLibraryRepository(tx).read_version_bundle(
                conn, item["skill_name"], item["base_version"],
            )
        diff = diff_skill_bundles(baseline, bundle)
        payload.update(
            candidate_skill=skill_payload(item["skill_name"], bundle),
            current_skill=skill_payload(item["skill_name"], baseline),
            candidate_skill_md=bundle["SKILL.md"].decode(),
            current_skill_md=baseline.get("SKILL.md", b"").decode(),
            bundle_diff=diff,
            skill_diff=next((f.get("diff", "") for f in diff["files"] if f["path"] == "SKILL.md"), ""),
        )
    return payload


async def version_context(conn, tx, name: str, version: int) -> dict:
    repo = CandidateRepository(tx)
    row = await conn.fetchrow(
        f"SELECT candidate_id FROM {tx.pool.schema}.skill_candidates "
        "WHERE skill_name=$1 AND published_version=$2 AND status='published' "
        "ORDER BY decided_at DESC LIMIT 1", name, version,
    )
    if not row:
        return {}
    payload = await candidate_payload(conn, tx, await repo.require(conn, row["candidate_id"]))
    return {
        key: payload.get(key) for key in (
            "job_id", "candidate_revision", "proposed_action", "rationale", "edit_summary",
            "evidence_classification", "decision", "evaluation", "skill_diff", "bundle_diff", "static_validation",
        )
    }
