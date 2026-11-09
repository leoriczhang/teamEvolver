"""Bound whole-Session Prompt windows without discarding new batch material."""
from __future__ import annotations

import copy
import hashlib
import json

from team_replay.datasets.synthesis import SynthesisInputTooLarge, evolution_synthesis_input
from team_skills.evolution.replay_case_synthesis import cross_session_synthesis_input
from team_skills.evolution.session_processing import evidence_source_key
from team_skills.library.repository import SkillLibraryError
from teamEvolver.evidence_context import EvidenceContextTooLarge

from .planner import prompt_editor_input
from .planning import synthesis_context
from .writer_runtime import MAX_EDITOR_INPUT_BYTES, EditorInputTooLarge, editor_messages, input_bytes

_CONTRACT = "teamevolver.template-input-contract.v1"
_HISTORY_WRITER_BYTES = MAX_EDITOR_INPUT_BYTES * 2 // 3


class PromptBatchInputTooLarge(SkillLibraryError):
    """This entire batch must be repackaged, never consumed as an ordinary skip."""

    def __init__(self, message, *, generation_cost=None):
        super().__init__(message)
        self.generation_cost = generation_cost


def shared_template_batch(context):
    """Only explicit new profiles use bounded history; old snapshots are unchanged."""
    policy = ((context.get("baseline_execution_plan") or {}).get("environment") or {}).get(
        "template_input_contract",
    )
    return isinstance(policy, dict) and policy.get("schema_version") == _CONTRACT


def _canonical(value):
    def plain(item):
        if isinstance(item, set):
            return sorted(item)
        raise TypeError(f"Unsupported material field: {type(item).__name__}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=plain)


def _source(session):
    provenance = session.get("provenance") or {}
    value = {"session_id": session["session_id"], "content_hash": session.get("content_hash"),
             "source_session_key": provenance.get("source_session_key"),
             "target_trace_refs": copy.deepcopy(provenance.get("target_trace_refs") or [])}
    value["projection_sha256"] = hashlib.sha256(_canonical(session).encode()).hexdigest()
    return value


def _measure(sessions, context):
    """Use the two real request builders and their original byte-limit rules."""
    measured = {"writer_input_bytes": None, "synthesis_input_bytes": None, "within_budget": False}
    context = copy.deepcopy(context)
    context.pop("material_window", None)
    context["total_evidence_sessions"] = len({evidence_source_key(session) for session in sessions})
    try:
        system, payload = prompt_editor_input(sessions, context)
        measured["writer_input_bytes"] = input_bytes(editor_messages(system, payload))
        options = cross_session_synthesis_input(
            skill_name=context.get("skill_name"), sessions=sessions, candidate_skill={},
            evidence_context=synthesis_context(context),
        )
        prepared = evolution_synthesis_input(**options)
        measured["synthesis_input_bytes"] = prepared["input_bytes"]
    except (EditorInputTooLarge, EvidenceContextTooLarge, SynthesisInputTooLarge) as exc:
        return {**measured, "native_size_error": str(exc)}
    return {**measured, "within_budget": True}


def select_prompt_materials(sessions, context, current_session_ids):
    """Keep every current Session; select complete history in repository order.

    The caller freezes the returned projection and audit before either model
    branch. It must not rerun this selection when restoring that checkpoint.
    Omitted history remains archived; it is neither truncated nor claimed read.
    """
    if not shared_template_batch(context):
        raise SkillLibraryError("bounded Prompt material selection requires an explicit template input contract")
    if (not isinstance(current_session_ids, (list, tuple)) or not current_session_ids
            or any(not isinstance(sid, str) or not sid for sid in current_session_ids)
            or len(set(current_session_ids)) != len(current_session_ids)):
        raise SkillLibraryError("current Prompt batch requires distinct nonempty Session IDs")
    ids = [session["session_id"] for session in sessions]
    if len(ids) != len(set(ids)) or not set(current_session_ids) <= set(ids):
        raise SkillLibraryError("current Prompt material is missing or duplicated")
    wanted = set(current_session_ids)
    current = [session for session in sessions if session["session_id"] in wanted]
    historical = [session for session in sessions if session["session_id"] not in wanted]
    measurement = _measure(current, context)
    if not measurement["within_budget"]:
        raise PromptBatchInputTooLarge("complete current Prompt batch exceeds an existing native request byte limit")
    selected, omitted = list(current), []
    for session in historical:
        proposed = _measure([*selected, session], context)
        if proposed["within_budget"] and proposed["writer_input_bytes"] <= _HISTORY_WRITER_BYTES:
            selected.append(session)
            measurement = proposed
        else:
            reason = ("historical_native_input_byte_budget" if not proposed["within_budget"]
                      else "historical_editor_workspace")
            omitted.append({**_source(session), "reason": reason})
    audit = {
        "schema_version": "teamevolver.prompt-material-window.v1",
        "measurement": "native_writer_json_and_synthesis_content_bytes_not_tokens",
        "historical_writer_input_budget": _HISTORY_WRITER_BYTES,
        "historical_editor_workspace_bytes": MAX_EDITOR_INPUT_BYTES - _HISTORY_WRITER_BYTES,
        "historical_order": "repository_latest_first_then_frozen_input_order",
        "current_sources": [_source(session) for session in current],
        "selected_historical_sources": [
            _source(session) for session in selected if session["session_id"] not in wanted
        ],
        "omitted_historical_sources": omitted,
        "selected_session_ids": [session["session_id"] for session in selected],
        "all_current_sessions_preserved": True,
        "historical_omissions_are_not_new_or_read_material": True,
        **measurement,
    }
    return copy.deepcopy(selected), audit
