"""Candidate read models and exact version provenance from PostgreSQL."""
from __future__ import annotations

from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.kernel.helpers import parse_skill_content
from team_skills.library.bundle import diff_skill_bundles, encode_bundle_payload
from team_skills.library.repository import SkillLibraryRepository


def skill_payload(name: str, bundle: dict, *, include_bundle: bool = True) -> dict | None:
    if not bundle:
        return None
    return {
        **parse_skill_content(name, bundle["SKILL.md"].decode("utf-8")),
        **({"bundle": encode_bundle_payload(bundle)} if include_bundle else {}),
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


def compact_evaluation(evaluation: dict | None) -> dict | None:
    """List rows carry verdicts and counts; full evidence stays in detail."""
    if not evaluation:
        return evaluation
    replay = evaluation.get("replay") or {}
    compact = {key: replay[key] for key in (
        "status", "verdict", "accepted", "reason", "error", "runtime_gate", "sampling",
        "efficiency", "no_regression", "total", "completed", "failed", "succeeded", "case_count",
    ) if key in replay}
    if not compact.get("error"):
        for case in replay.get("cases") or []:
            error = (case.get("baseline") or {}).get("error") or (case.get("candidate") or {}).get("error")
            if error:
                compact["error"] = error
                break
    checklist = {
        side: {key: value for key, value in report.items() if key in (
            "all_satisfied", "case_count", "total", "satisfied_count", "unmet_count", "judge",
        )}
        for side, report in (replay.get("checklist") or {}).items()
    }
    if checklist:
        compact["checklist"] = checklist
    if replay.get("decision_policy"):
        compact["decision_policy"] = {
            key: value for key, value in replay["decision_policy"].items() if key != "checklist"
        }
    return {
        **{key: evaluation[key] for key in (
            "skill_name", "proposed_action", "recommended_publish", "candidate_revision",
            "status", "static_validation",
        ) if key in evaluation},
        "replay": compact, "summary_only": True,
    }


def checklist_evidence(report: dict) -> dict:
    """Keep checklist judgements without embedded traces or media artifacts."""
    result = {key: value for key, value in report.items() if key in (
        "all_satisfied", "case_count", "total", "satisfied_count", "unmet_count", "judge", "rounds",
    )}
    if "items" in report:
        result["items"] = [
            {key: value for key, value in item.items() if key in ("id", "text", "kind", "satisfied", "evidence")}
            for item in report["items"]
        ]
    if "reports" in report:
        result["reports"] = [checklist_evidence(item) for item in report["reports"]]
    return result


def detail_evaluation(evaluation: dict | None) -> dict | None:
    """Load review evidence first; replay transcripts are explicitly requested."""
    result = compact_evaluation(evaluation)
    if not result:
        return result
    result.pop("summary_only", None)
    result.update(cached=True, replay_details_omitted=True)
    replay = evaluation.get("replay") or {}
    if replay.get("checklist"):
        result["replay"]["checklist"] = {
            side: checklist_evidence(report) for side, report in replay["checklist"].items()
        }
    result["replay"]["case_count"] = len(replay.get("cases") or [])
    return result


def evolution_feedback(evaluation: dict | None) -> dict:
    """Give planners verdict evidence without reprocessing old Replay bodies."""
    result = detail_evaluation(evaluation) or {}
    if not result:
        return result
    cases = []
    for case in (evaluation.get("replay") or {}).get("cases") or []:
        item = {key: case[key] for key in (
            "case_id", "dataset_id", "dataset_name", "selection", "evidence_window", "query",
        ) if key in case}
        for side in ("baseline", "candidate"):
            branch = case.get(side) or {}
            item[side] = {key: branch[key] for key in (
                "status", "ok", "completed", "error", "error_code", "session_id", "trace_ids",
                "interaction_turns", "tool_call_count", "input_tokens", "output_tokens",
                "total_tokens", "elapsed_seconds", "metrics_incomplete_reasons",
            ) if key in branch}
            if branch.get("checklist_report"):
                item[side]["checklist_report"] = checklist_evidence(branch["checklist_report"])
        cases.append(item)
    result["replay"]["cases"] = cases
    return result


async def candidate_payload(conn, tx, item: dict, *, compact: bool = False, include_replay: bool = True) -> dict:
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
    if not compact:
        payload["evaluation"] = (
            {**item["evaluation"], "cached": True} if include_replay and item["evaluation"]
            else detail_evaluation(item["evaluation"])
        )
    if not include_replay and isinstance(payload.get("decision"), dict):
        payload["decision"] = {
            key: value for key, value in payload["decision"].items() if key != "evaluation"
        }
    if item["status"] == "validating":
        from team_skills.evolution.repository import EvolutionRepository
        command = await EvolutionRepository(tx).command(conn, item["validate_task_id"])
        frozen = command["payload"].get("detail", detail) if command else detail
        payload["replay_progress"] = {
            "total": len(frozen.get("replay_cases") or []),
            **await ReplayRepository(tx).progress(conn, "skill", item["validate_task_id"]),
        }
        payload["replay_sampling"] = frozen.get("replay_sampling") or {}
    if compact:
        for key in ("bundle_diff", "skill_diff", "file_changes", "candidate_skill", "current_skill",
                    "candidate_skill_md", "current_skill_md"):
            payload.pop(key, None)
        payload["evaluation"] = compact_evaluation(item["evaluation"])
    else:
        bundle = await repo.read_bundle(conn, item["candidate_id"])
        baseline = {}
        if item["base_version"]:
            baseline = await SkillLibraryRepository(tx).read_version_bundle(
                conn, item["skill_name"], item["base_version"],
            )
        diff = diff_skill_bundles(baseline, bundle)
        payload.update(
            candidate_skill=skill_payload(item["skill_name"], bundle, include_bundle=include_replay),
            current_skill=skill_payload(item["skill_name"], baseline, include_bundle=include_replay),
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
