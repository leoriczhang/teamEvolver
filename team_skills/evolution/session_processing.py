"""Reuse mandatory ingest analysis; never run a second Session Judge."""
from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from datetime import datetime, timezone

from session_ingestion.schemas import session_payload_hash
from team_skills.evolution.stages.aggregate import aggregate_sessions_by_skill
from team_skills.evolution.stages.analyze import session_has_merged_outputs
from team_skills.evolution.stages.summarize import _extract_session_metadata, build_session_trajectory


async def load_sessions(descriptors: list[dict], payloads) -> list[dict]:
    sessions = []
    for descriptor in descriptors:
        raw = await asyncio.to_thread(payloads.get_session, descriptor["content_ref"])
        if session_payload_hash(raw) != descriptor["content_hash"]:
            raise ValueError("Session content hash changed after acceptance")
        session = copy.deepcopy(raw)
        if not session_has_merged_outputs(session):
            raise ValueError("Session requires mandatory ingest analysis before Skill Evolution")
        session.update({
            "session_id": descriptor["session_id"], "content_hash": descriptor["content_hash"],
            "user_alias": descriptor["user_id"],
        })
        _extract_session_metadata(session)
        session["_trajectory"] = build_session_trajectory(session)
        session["_avg_prm"] = session["_judge_scores"]["overall_score"]
        sessions.append(session)
    return eligible_evidence(sessions)


def _generated(session):
    context = session.get("runtime_context") or {}
    provenance = session.get("provenance") or {}
    source = str(session.get("source") or "").lower()
    if context.get("candidate_job_id") or context.get("replay_run_id") or context.get("replay_request_id"):
        return True
    if source in {"managed_agent_candidate_audit", "replay", "true_replay", "synthetic", "generated"}:
        return True
    return any(
        container.get("synthetic") is True or container.get("is_synthetic") is True
        or str(container.get("origin") or container.get("kind") or "").lower() in {"replay", "synthetic", "generated"}
        for container in (session, context, provenance, session.get("meta") or {}, session.get("metadata") or {})
        if isinstance(container, Mapping)
    )


def _source_scope(session):
    """Absent legacy provenance remains compatible; explicit uncertainty never does."""
    if _generated(session):
        return "", []
    if "provenance" not in session:
        meta = session.get("metadata") or {}
        root = meta.get("split_from_session") or (session.get("meta") or {}).get("split_from_session")
        root = str(root or session.get("session_id") or "")
        return ("legacy:" + root if root else ""), None
    provenance = session.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("schema_version") != "session-source.v1":
        return "", []
    key = provenance.get("source_session_key")
    if not isinstance(key, str) or not key or provenance.get("identity_verified") is not True or (
        provenance.get("trace_identity_verified") is not True or provenance.get("identity_conflict")
        or provenance.get("revision_conflicts")
    ):
        return "", []
    revisions = provenance.get("trace_revisions") or []
    targets = provenance.get("target_trace_refs") or []
    if not isinstance(revisions, list) or not isinstance(targets, list):
        return "", []
    refs = []
    for ref in targets:
        if not isinstance(ref, Mapping) or ref.get("source_session_key") != key or any(
            not isinstance(ref.get(field), str) or not ref[field]
            for field in ("trace_id", "revision", "context_revision")
        ):
            continue
        if ref not in revisions:
            continue
        if "turns" in session and not any(_trace_id(turn) == ref["trace_id"] for turn in session.get("turns") or []):
            continue
        if dict(ref) not in refs:
            refs.append(dict(ref))
    return (key, refs) if refs else ("", [])


def _trace_id(turn):
    return str(turn.get("source_trace_id") or turn.get("trace_id") or "")


def evidence_source_key(session: dict) -> str:
    """Independent upstream root, or an empty string when this is not valid evidence."""
    return _source_scope(session)[0]


def _target_projection(session, refs):
    result = copy.deepcopy(session)
    if refs is None:
        return result
    result["provenance"]["target_trace_refs"] = copy.deepcopy(refs)
    targets = {ref["trace_id"] for ref in refs}
    context_refs = []
    for ref in [*(result["provenance"].get("context_trace_refs") or []),
                *(result["provenance"].get("trace_revisions") or [])]:
        if ref["trace_id"] not in targets and ref not in context_refs:
            context_refs.append(copy.deepcopy(ref))
    result["provenance"]["context_trace_refs"] = context_refs
    if "turns" in result:
        view = {"turns": [copy.deepcopy(turn) for turn in result["turns"] if _trace_id(turn) in targets]}
        result["_evidence_turns"] = copy.deepcopy(view["turns"])
        _extract_session_metadata(view)
        for field in ("_skills_referenced", "_skills_reference_source", "_skills_injected", "_has_tool_errors",
                      "_verified_skill_feedback"):
            result[field] = view[field]
        result["_trajectory"] = build_session_trajectory(view)
    return result


def _source_time(session, identity):
    for turn in session.get("turns") or []:
        if _trace_id(turn) != identity or not turn.get("source_updated_at"):
            continue
        try:
            value = datetime.fromisoformat(str(turn["source_updated_at"]).replace("Z", "+00:00"))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def eligible_evidence(sessions: list[dict]) -> list[dict]:
    """Deduplicate factual Trace units, keeping their full snapshots only as Context.

    Prefer factual upstream timestamps; archive completion can arrive out of order.
    Without timestamps use frozen input order, never hash ordering. Distinct target
    Traces in split segments are retained.
    This does not create another independent root or recompute any Judge score.
    """
    scopes = [(session, *_source_scope(session)) for session in sessions]
    owners = {}
    for index, (session, key, refs) in enumerate(scopes):
        for ref in refs or []:
            identity = key, ref["trace_id"]
            existing = owners.get(identity)
            observed_at = _source_time(session, ref["trace_id"])
            if existing and observed_at is not None and existing[2] is not None and observed_at < existing[2]:
                continue
            if existing is None or existing[1] != ref:
                owners[identity] = index, ref, observed_at
    result, legacy = [], set()
    for index, (session, key, refs) in enumerate(scopes):
        if not key:
            continue
        if refs is None:
            identity = str(session.get("session_id") or "")
            if identity in legacy:
                continue
            legacy.add(identity)
            result.append(_target_projection(session, None))
            continue
        selected = [ref for ref in refs if owners[(key, ref["trace_id"])][0] == index]
        if selected:
            result.append(_target_projection(session, selected))
    return result


def merge_evidence(current: list[dict], historical: list[dict]) -> list[dict]:
    """Current roots replace historical copies; historical roots contribute once."""
    recent = eligible_evidence(current)
    roots = {evidence_source_key(session) for session in recent}
    result = list(recent)
    for session in historical:
        key = evidence_source_key(session)
        if key and key not in roots:
            projection = eligible_evidence([session])
            if projection:
                result.extend(projection)
                roots.add(key)
    return result


def project_prompt_session(session: dict, baseline) -> dict | None:
    """Select this profile's verified targets while retaining one original root.

    Topic splitting does not guarantee profile/version boundaries. A continuous
    Session can therefore contribute different target Traces to distinct
    profiles without duplicating its independent source identity. Other turns
    remain Context and never become allowed Evidence for this projection.
    """
    from team_skills.execution.evolution import prompt_session_matches

    if prompt_session_matches(session, baseline):
        return copy.deepcopy(session)
    key, refs = _source_scope(session)
    if not key or not refs:
        return None
    selected = [ref for ref in refs if prompt_session_matches(_target_projection(session, [ref]), baseline)]
    if not selected:
        return None
    projected = _target_projection(session, selected)
    # Full turns remain available as explicitly marked Context in the writer.
    # Do not leave another profile's messages in the target message collection.
    projected["messages"] = [copy.deepcopy(message) for turn in projected.get("_evidence_turns", [])
                             for message in turn.get("messages") or []]
    return projected


def groups(sessions: list[dict], *, execution_profile=None, baseline=None) -> dict[str, list[dict]]:
    sessions = eligible_evidence(sessions)
    if execution_profile is not None and not execution_profile["affected_skill_ids"]:
        # The selected pure-Prompt profile is the only requested asset scope.
        # An incidental Skill read must not turn it into a Skill mutation job.
        matched = [projection for session in sessions
                   if (projection := project_prompt_session(session, baseline)) is not None]
        return {"execution:" + execution_profile["profile_id"]: matched} if matched else {}
    return aggregate_sessions_by_skill(sessions)


def evidence_gap(sessions: list[dict], config) -> str:
    sessions = eligible_evidence(sessions)
    users = {s.get("user_alias") for s in sessions if s.get("user_alias")}
    ids = {evidence_source_key(s) for s in sessions}
    if len(ids) < int(config.get("min_group_sessions")):
        return "insufficient distinct Sessions for team evidence"
    if len(users) < int(config.get("min_group_users")):
        return "insufficient distinct users for team evidence"
    return ""


def replay_cases(sessions: list[dict]) -> list[dict]:
    cases = []
    for session in eligible_evidence(sessions):
        context = session.get("runtime_context") or {}
        if context.get("candidate_job_id"):
            continue
        for turn in session.get("_evidence_turns", session.get("turns")) or []:
            if not turn.get("prompt_text") or not turn.get("response_text"):
                continue
            cases.append({
                "case_id": f"{session['session_id']}:{int(turn.get('turn_num') or 0)}",
                "query": str(turn["prompt_text"])[:3000], "skill_ids": [],
                "checks": [{"id": "R01", "kind": "output", "text": str(turn["prompt_text"])[:3000]}],
                "materials": [], "provenance": {
                    "session_id": session["session_id"], "turn_num": int(turn.get("turn_num") or 0),
                    "session_snapshot": {
                        key: copy.deepcopy(session[key])
                        for key in ("session_id", "turns", "runtime", "runtime_context", "source")
                        if key in session
                    },
                    "source_runtime": copy.deepcopy(session.get("runtime") or {"type": session.get("source", "")}),
                    "source_runtime_context": copy.deepcopy(context),
                    "evidence_window": session.get("_evidence_window") or "recent",
                    "source_session_key": evidence_source_key(session),
                    "target_trace_refs": [copy.deepcopy(ref) for ref in (session.get("provenance") or {}).get(
                        "target_trace_refs", []) if ref.get("trace_id") == _trace_id(turn)],
                },
                "replay": {"progressive_disclosure": {"batch_size": 4}},
                "metadata": {"evaluation_profile": str(context.get("evaluation_profile") or "")},
            })
            if len(cases) == 6:
                return cases
    return cases
