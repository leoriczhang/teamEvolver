"""Cross-Session synthesis of the configured current Candidate Replay Cases."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping

from team_replay.datasets.synthesis import synthesize_evolution_datasets
from team_replay.execution_plan import ExecutionPlanError
from team_skills.execution.inputs import (
    execution_input_snapshot,
    has_template_input_contract,
    verified_source_execution_inputs,
)

from .session_processing import evidence_source_key, merge_evidence

CURRENT_CASE_COUNT = 2
MIN_CHECKS = 6
MAX_CHECKS = 8
MIN_SOURCE_SESSIONS = 2


class CrossSessionCaseSynthesisError(ValueError):
    pass


def cross_session_synthesis_input(*, skill_name, sessions, candidate_skill, evidence_context):
    """Share the existing host inputs/options with request-size inspection."""
    sessions = merge_evidence(sessions, [])
    roots = {evidence_source_key(session) for session in sessions}
    context = {**copy.deepcopy(evidence_context), "independent_source_sessions": len(roots),
               "source_session_keys": {session["session_id"]: evidence_source_key(session) for session in sessions}}
    count = (context.get("replay_case_counts") or {}).get("current", CURRENT_CASE_COUNT)
    if type(count) is not int or not 1 <= count <= 6:
        raise CrossSessionCaseSynthesisError("current Replay Case count must be an integer in 1..6")
    return {"skill_name": skill_name, "sessions": sessions, "candidate_skill": candidate_skill,
            "evidence_context": context, "case_count": count, "min_requirements": MIN_CHECKS,
            "max_requirements": MAX_CHECKS, "min_source_sessions": MIN_SOURCE_SESSIONS, "batch_size": 2}


class LLMCrossSessionCaseSynthesizer:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, *, skill_name, sessions, candidate_skill, evidence_context):
        inputs = cross_session_synthesis_input(
            skill_name=skill_name, sessions=sessions, candidate_skill=candidate_skill,
            evidence_context=evidence_context,
        )
        sessions, evidence_context = inputs["sessions"], inputs["evidence_context"]
        if evidence_context["independent_source_sessions"] < MIN_SOURCE_SESSIONS:
            raise CrossSessionCaseSynthesisError(
                "cross-Session synthesis requires at least two independent source Sessions",
            )
        generated = await synthesize_evolution_datasets(self.llm, **inputs)
        return hydrate_cross_session_cases(
            generated, sessions, skill_name=skill_name,
            baseline=(evidence_context or {}).get("baseline_execution_plan"),
            case_count=inputs["case_count"],
        )


def _anchor_source_projection(anchor, source_ids, sources, query):
    if not isinstance(anchor, Mapping):
        raise CrossSessionCaseSynthesisError("Replay evidence anchor must be an object")
    source_id, trace_id = anchor.get("session_id"), anchor.get("trace_id")
    if source_id not in source_ids or source_id not in sources:
        raise CrossSessionCaseSynthesisError("Replay evidence anchor is outside its source Sessions")
    if not isinstance(trace_id, str) or not trace_id:
        raise CrossSessionCaseSynthesisError("Replay evidence anchor requires its recorded target Trace")
    source = sources[source_id]
    provenance = source.get("provenance") or {}
    target_refs = provenance.get("target_trace_refs") or []
    context_refs = provenance.get("context_trace_refs") or []
    targets = {ref.get("trace_id") for ref in target_refs if isinstance(ref, Mapping)}
    contexts = {ref.get("trace_id") for ref in context_refs if isinstance(ref, Mapping)}
    if (target_refs and trace_id not in targets) or (trace_id in contexts and trace_id not in targets):
        raise CrossSessionCaseSynthesisError("Replay evidence anchor is not a current target Trace")
    turns = source.get("_evidence_turns", source.get("turns", []))
    matches = [
        turn for turn in turns
        if isinstance(turn, Mapping) and str(turn.get("source_trace_id") or turn.get("trace_id") or "") == trace_id
    ]
    if len(matches) != 1:
        raise CrossSessionCaseSynthesisError("Replay evidence anchor requires exactly one recorded target turn")
    turn = matches[0]
    recorded_query = turn.get("prompt_text") or turn.get("instruction") or turn.get("user_query") or ""
    if (not isinstance(recorded_query, str) or query != recorded_query
            or anchor.get("recorded_query_sha256") != hashlib.sha256(recorded_query.encode()).hexdigest()):
        raise CrossSessionCaseSynthesisError("Replay evidence anchor does not match its recorded target query")
    projection = copy.deepcopy(source)
    projection["turns"] = [copy.deepcopy(turn)]
    projection["_evidence_turns"] = [copy.deepcopy(turn)]
    projected_provenance = projection.setdefault("provenance", {})
    projected_provenance["target_trace_refs"] = [
        copy.deepcopy(ref) for ref in target_refs if isinstance(ref, Mapping) and ref.get("trace_id") == trace_id
    ]
    projected_provenance["context_trace_refs"] = []
    if "trace_revisions" in projected_provenance:
        projected_provenance["trace_revisions"] = [
            ref for ref in projected_provenance["trace_revisions"]
            if isinstance(ref, Mapping) and ref.get("trace_id") == trace_id
        ]
    projected_provenance["replay_anchor_projection"] = {
        "anchor_id": anchor.get("anchor_id"), "source_session_id": source_id, "trace_id": trace_id,
    }
    return projection


def hydrate_cross_session_cases(cases, sessions, *, skill_name, baseline=None, case_count=CURRENT_CASE_COUNT):
    if type(case_count) is not int or not 1 <= case_count <= 6:
        raise CrossSessionCaseSynthesisError("current Replay Case count must be an integer in 1..6")
    if len(cases) != case_count:
        count = "two" if case_count == 2 else str(case_count)
        raise CrossSessionCaseSynthesisError(f"Skill Candidate requires exactly {count} cross-Session Replay Cases")
    sources = {
        str(session.get("session_id") or ""): session
        for session in merge_evidence(sessions, []) if str(session.get("session_id") or "").strip()
    }
    hydrated = []
    for raw in cases:
        case = copy.deepcopy(dict(raw))
        provenance = case.setdefault("provenance", {})
        source_ids = list(dict.fromkeys(
            str(value or "").strip() for value in provenance.get("session_ids") or []
            if str(value or "").strip() in sources
        ))
        roots = {evidence_source_key(sources[source_id]) for source_id in source_ids}
        if len(roots) < MIN_SOURCE_SESSIONS:
            raise CrossSessionCaseSynthesisError(
                "each current Replay Case requires at least two independent source Sessions",
            )
        anchor = provenance.get("evidence_anchor")
        if anchor is not None:
            representative = _anchor_source_projection(anchor, source_ids, sources, case.get("query"))
        else:
            representative = next((
                sources[source_id] for source_id in source_ids
                if isinstance(sources[source_id].get("turns"), list) and sources[source_id].get("turns")
            ), None)
        if representative is None:
            raise CrossSessionCaseSynthesisError("Replay Case has no executable source Session")
        checks = case.get("checks") if isinstance(case.get("checks"), list) else []
        if not MIN_CHECKS <= len(checks) <= MAX_CHECKS:
            raise CrossSessionCaseSynthesisError("current Replay Case Checklist must contain 6..8 items")
        runtime_context = (
            representative.get("runtime_context")
            if isinstance(representative.get("runtime_context"), Mapping)
            else {}
        )
        first_turn = (representative.get("_evidence_turns") or representative["turns"])[0]
        source_runtime = copy.deepcopy(representative.get("runtime") or {})
        observed_binding = (first_turn.get("request_context") or {}).get("execution_binding") or {}
        if (not source_runtime.get("type") and observed_binding.get("evidence_level") == "upstream_metadata"
                and isinstance(observed_binding.get("runtime_type"), str)):
            source_runtime["type"] = observed_binding["runtime_type"]
        materialization = provenance.get("query_materialization")
        if (isinstance(materialization, Mapping) and "execution_inputs_sha256" in materialization
                and not has_template_input_contract(baseline)):
            raise CrossSessionCaseSynthesisError("variable-bound Replay sources require a frozen shared-template plan")
        if has_template_input_contract(baseline):
            if anchor is None:
                raise CrossSessionCaseSynthesisError("shared-template Replay Case requires an original target anchor")
            try:
                inputs = verified_source_execution_inputs(representative, first_turn, baseline)
            except (ExecutionPlanError, KeyError, TypeError, ValueError) as exc:
                raise CrossSessionCaseSynthesisError("shared-template Replay source inputs are not verified") from exc
            existing = provenance.get("execution_inputs")
            if existing is not None and existing != inputs:
                raise CrossSessionCaseSynthesisError("Replay Case input bindings differ from its original target")
            if materialization is not None and materialization.get("execution_inputs_sha256") != hashlib.sha256(
                json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode(),
            ).hexdigest():
                raise CrossSessionCaseSynthesisError("Replay Case input bindings differ from its frozen query receipt")
            provenance["execution_inputs"] = inputs
        provenance.update({
            "session_ids": source_ids,
            "session_id": str(representative.get("session_id") or ""),
            "turn_num": max(1, int(first_turn.get("turn_num") or 1)),
            "session_snapshot": {
                key: copy.deepcopy(representative[key])
                for key in ("session_id", "turns", "runtime", "runtime_context", "source", "provenance")
                if key in representative
            },
            "source_runtime": copy.deepcopy(
                source_runtime or {"type": representative.get("source", "")}
            ),
            "source_runtime_context": copy.deepcopy(runtime_context),
            "evidence_window": "recent",
            "synthesis_mode": "cross_session",
            "source_session_keys": sorted(roots),
            "target_trace_refs": (
                copy.deepcopy((representative.get("provenance") or {}).get("target_trace_refs") or [])
                if anchor is not None else [
                    copy.deepcopy(ref) for source_id in source_ids
                    for ref in (sources[source_id].get("provenance") or {}).get("target_trace_refs") or []
                ]
            ),
        })
        if has_template_input_contract(baseline):
            provenance["session_snapshot"] = execution_input_snapshot(inputs)
        case["skill_ids"] = [skill_name] if skill_name else []
        hydrated.append(case)
    return hydrated
