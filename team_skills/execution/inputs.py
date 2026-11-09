"""Verify shared-template source bindings against the frozen original request.

Variable values and public input messages are recorded task inputs, not assets
that the Prompt writer may edit. Source identity and exact request bytes still
decide attribution; template similarity is never a substitute for provenance.
"""
from __future__ import annotations

import copy

from team_replay.execution_plan import ExecutionPlanError
from team_replay.source_inputs import has_template_input_contract as has_template_input_contract
from team_replay.source_inputs import verified_source_execution_inputs
from team_replay.variable_context import applicable_component_ids


def execution_input_snapshot(inputs):
    """Retain source input identity without copying the old answer or grade."""
    source = inputs["source_ref"]
    reference = {key: value for key, value in source.items() if key != "session_id"}
    return {
        "session_id": source["session_id"],
        "provenance": {"source_session_key": source["source_session_key"], "target_trace_refs": [reference]},
        "turns": [{"source_trace_id": source["trace_id"], "request_context": {
            "template_inputs": copy.deepcopy({key: value for key, value in inputs.items() if key != "source_ref"}),
        }}],
    }


def component_source_bindings(sessions, baseline):
    """Attribute components to verified target inputs, never to profile defaults."""
    result = []
    for session in sessions:
        turns = session.get("_evidence_turns", session.get("turns")) or []
        for turn in turns:
            inputs = verified_source_execution_inputs(session, turn, baseline)
            if inputs is None:
                raise ExecutionPlanError("variable Context attribution requires verified source inputs")
            result.append({
                "source_ref": copy.deepcopy(inputs["source_ref"]),
                "active_component_ids": sorted(applicable_component_ids(baseline, inputs["template_variables"])),
            })
    return result


def shared_template_session_matches(session, baseline):
    return shared_template_match_report(session, baseline)["matched"]


def shared_template_match_report(session, baseline):
    """Explain why an original source can or cannot enter this registered profile.

    The report is read-only and does not grant ingestion/source privileges.
    Matching components are derived only after exact request verification.
    """
    turns = session.get("_evidence_turns", session.get("turns")) or []
    if not isinstance(turns, list) or not turns:
        return {"matched": False, "reason": "source has no target turns", "targets": []}
    targets = []
    try:
        for turn in turns:
            inputs = verified_source_execution_inputs(session, turn, baseline)
            if inputs is None:
                raise ExecutionPlanError("registered profile has no shared-template input contract")
            targets.append({
                "source_ref": copy.deepcopy(inputs["source_ref"]),
                "active_component_ids": sorted(applicable_component_ids(baseline, inputs["template_variables"])),
            })
    except (ExecutionPlanError, KeyError, TypeError, ValueError) as exc:
        return {"matched": False, "reason": str(exc), "targets": []}
    return {"matched": True, "reason": "original request matches registered template and inputs", "targets": targets}
