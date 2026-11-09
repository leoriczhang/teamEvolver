"""Lossless, boundary-aware presentation of recorded Evidence, without judging it.

Import containers do not establish historical conversation continuity. This
module keeps that distinction explicit for consumers while preserving the
original source identities, request text and external feedback.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping

MAX_EVIDENCE_BYTES = 786_432
_RELATION_FIELDS = (
    "origin", "constructed_import_batch", "batch_id", "original_session_independence",
    "actual_original_request_observed", "conversation_relationship",
)
_CONTENT_FIELDS = (
    "recorded_query", "recorded_response", "recorded_messages", "recorded_context", "source_feedback",
)


class EvidenceContextError(ValueError):
    """Recorded Evidence cannot be presented completely and unambiguously."""


class EvidenceContextTooLarge(EvidenceContextError):
    """Complete recorded Evidence exceeds the existing consumer byte limit."""


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceContextError("Evidence context must contain standard JSON values") from exc


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _content_bytes(value):
    return value.encode() if isinstance(value, str) else _json(value).encode()


def _turns(session):
    value = session.get("_evidence_turns", session.get("turns", []))
    if not isinstance(value, list):
        raise EvidenceContextError("Evidence turns must be a list")
    if any(not isinstance(turn, Mapping) for turn in value):
        raise EvidenceContextError("Evidence turns must contain recorded objects")
    return value


def _trace_id(turn):
    return str(turn.get("source_trace_id") or turn.get("trace_id") or "")


def _trace_refs(session, field):
    provenance = session.get("provenance") or {}
    return {str(ref.get("trace_id") or "") for ref in provenance.get(field) or []
            if isinstance(ref, Mapping) and ref.get("trace_id")}


def _target_turns(session):
    targets = _trace_refs(session, "target_trace_refs")
    context = _trace_refs(session, "context_trace_refs")
    return [turn for turn in _turns(session)
            if (not targets or _trace_id(turn) in targets)
            and (_trace_id(turn) not in context or _trace_id(turn) in targets)]


def _reconstruction_views(turn):
    request = turn.get("request_context") or {}
    if not isinstance(request, Mapping):
        raise EvidenceContextError("Recorded request context must be an object")
    observations = []
    trace_input = request.get("trace_input")
    if isinstance(trace_input, Mapping) and isinstance(trace_input.get("reconstruction"), Mapping):
        observations.append(("request_context.trace_input.reconstruction", trace_input["reconstruction"]))
    for index, generation in enumerate(request.get("generation_requests") or []):
        value = generation.get("input") if isinstance(generation, Mapping) else None
        if isinstance(value, Mapping) and isinstance(value.get("reconstruction"), Mapping):
            observations.append((f"request_context.generation_requests[{index}].input.reconstruction",
                                 value["reconstruction"]))
    return observations


def _reconstruction(turn):
    observations = _reconstruction_views(turn)
    merged = {}
    for _, value in observations:
        for key in _RELATION_FIELDS:
            if key not in value:
                continue
            if key in merged and merged[key] != value[key]:
                raise EvidenceContextError(f"Conflicting recorded reconstruction field: {key}")
            merged[key] = copy.deepcopy(value[key])
    return merged, [path for path, _ in observations]


def _isolated(reconstruction):
    return (reconstruction.get("constructed_import_batch") is True
            or reconstruction.get("conversation_relationship") == "independent_samples")


def has_isolated_records(sessions):
    """Inspect only explicit declarations; never infer boundaries from IDs or Tags."""
    isolated = False
    for session in sessions:
        for turn in _target_turns(session):
            # Legacy Skill synthesis did not interpret reconstruction metadata.
            # Only an explicit batch declaration activates its extra contract.
            request = turn.get("request_context") or {}
            if not isinstance(request, Mapping):
                continue
            if not any(_isolated(value) for _, value in _reconstruction_views(turn)):
                continue
            reconstruction, _ = _reconstruction(turn)
            isolated = _isolated(reconstruction) or isolated
    return isolated


def _reject_json_constant(value):
    raise ValueError(f"Nonstandard JSON constant: {value}")


def _structured_feedback(text):
    start = text.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(text[start:], parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _feedback(turn, *, isolated):
    messages = turn.get("messages") or []
    assistant_positions = [index for index, message in enumerate(messages)
                           if isinstance(message, Mapping) and message.get("role") == "assistant"]
    if not assistant_positions:
        return []
    tags = (turn.get("_langfuse") or {}).get("tags") or []
    feedback = []
    for index in range(assistant_positions[-1] + 1, len(messages)):
        message = messages[index]
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        text = message.get("content")
        if not isinstance(text, str):
            continue
        declared_machine = isolated and "machine-evaluation-feedback" in tags and "model_eval_feedback" in text
        structured = _structured_feedback(text) if declared_machine else None
        feedback.append({
            "origin": "declared_external_model_evaluation" if declared_machine else "recorded_user_message",
            "observed_role": "user", "raw_text": text,
            "structured_payload": structured,
            "parse_status": "parsed" if structured is not None else "unparsed",
            "source_path": f"messages[{index}]", "independently_verified": False,
        })
    return feedback


def _recorded_example(turn, *, sid, key, reconstruction, paths, isolated):
    tid = _trace_id(turn)
    request = turn.get("request_context") or {}
    trace_input = request.get("trace_input") or {}
    if not isinstance(trace_input, Mapping):
        trace_input = {}
    query = turn.get("prompt_text") or turn.get("instruction") or turn.get("user_query") or ""
    response = turn.get("response_text") or ""
    if not isinstance(query, str) or not isinstance(response, str):
        raise EvidenceContextError("Recorded query and response must be text")
    example = {
        "example_id": "example-" + _digest(list(key))[:24], "session_id": sid,
        "trace_id": tid or None, "evidence_ref": tid or sid,
        "relationship": "isolated_record" if isolated else "recorded_session_turn",
        "sequence_semantics": "continuity_not_asserted" if isolated else "recorded_session_order",
        "request_observation": {
            "declarations": reconstruction, "source_paths": paths,
            "evidence_level": request.get("evidence_level", "unknown"),
            "original_session_independence": reconstruction.get("original_session_independence", "unknown"),
        },
        "recorded_query": query, "recorded_response": response,
        "recorded_messages": copy.deepcopy(turn.get("messages") or []),
        "recorded_context": {key: copy.deepcopy(value) for key, value in trace_input.items()
                             if key not in {"messages", "reconstruction"}},
        "source_feedback": _feedback(turn, isolated=isolated),
    }
    if "template_inputs" in request and "template_inputs" not in example["recorded_context"]:
        example["recorded_context"]["template_inputs"] = copy.deepcopy(request["template_inputs"])
    if isolated and not query.strip():
        raise EvidenceContextError("An isolated record requires its complete original query")
    return example


def _reference(ref, source_key):
    if not isinstance(ref, Mapping) or ref.get("source_session_key") != source_key:
        return None
    if any(not isinstance(ref.get(field), str) or not ref[field]
           for field in ("trace_id", "revision", "context_revision")):
        return None
    return copy.deepcopy(dict(ref))


def _reference_map(refs, source_key):
    result = {}
    for value in refs or []:
        ref = _reference(value, source_key)
        if ref is None:
            continue
        tid = ref["trace_id"]
        if tid in result and result[tid] != ref:
            raise EvidenceContextError("Conflicting recorded Context Trace references")
        result[tid] = ref
    return result


def _recorded_facts(turn):
    # Split-local numbering and platform PRM annotations do not revise upstream facts.
    return {name: value for name, value in turn.items() if name not in {"turn_num", "prm_score"}}


def _context_records(sessions, target_fingerprints):
    """Only explicitly declared, server-scoped Context can supplement targets.

    The containing Session and its analysis snapshot must both declare the same
    Context TraceRef. Those source attestations are copied, not reinterpreted as
    new independent Evidence. A Context record cannot activate the legacy Skill
    path when its selected target records have no isolated-record declaration.
    """
    contexts, seen, references = [], {}, {}
    target_references, target_records = {}, {}
    for session in sessions:
        provenance = session.get("provenance") or {}
        source_key = provenance.get("source_session_key")
        if not isinstance(source_key, str) or not source_key:
            continue
        for value in provenance.get("target_trace_refs") or []:
            ref = _reference(value, source_key)
            if ref is not None and (source_key, ref["trace_id"]) in target_fingerprints:
                target_references.setdefault((source_key, ref["trace_id"]), []).append(ref)
        for turn in _target_turns(session):
            key = source_key, _trace_id(turn)
            raw = _recorded_facts(turn)
            target_records.setdefault(key, []).append(_digest(raw))
    for session in sessions:
        if not has_isolated_records([session]):
            continue
        provenance = session.get("provenance") or {}
        analysis = session.get("analysis_context") or {}
        if not isinstance(provenance, Mapping) or not isinstance(analysis, Mapping):
            continue
        source_key = provenance.get("source_session_key")
        if (provenance.get("schema_version") != "session-source.v1"
                or not isinstance(source_key, str) or not source_key
                or provenance.get("identity_verified") is not True
                or provenance.get("trace_identity_verified") is not True
                or provenance.get("identity_conflict") or provenance.get("revision_conflicts")
                or analysis.get("context_only") is not True):
            continue
        declared = _reference_map(provenance.get("context_trace_refs"), source_key)
        observed = _reference_map(analysis.get("context_trace_refs"), source_key)
        refs = {}
        for tid, ref in declared.items():
            if tid not in observed:
                continue
            if observed[tid] != ref:
                raise EvidenceContextError("Conflicting recorded Context Trace references")
            key = source_key, tid
            if any(target != ref for target in target_references.get(key, [])):
                raise EvidenceContextError("Conflicting target and Context Trace references")
            if key in references and references[key] != ref:
                raise EvidenceContextError("Conflicting recorded Context Trace references")
            references[key] = ref
            refs[tid] = ref
        if not refs:
            continue
        turns = analysis.get("turns") or []
        if not isinstance(turns, list) or any(not isinstance(turn, Mapping) for turn in turns):
            raise EvidenceContextError("Recorded analysis Context turns must be a list of objects")
        found = set()
        for turn in turns:
            tid = _trace_id(turn)
            if tid not in refs:
                continue
            found.add(tid)
            if not any(_isolated(value) for _, value in _reconstruction_views(turn)):
                continue
            reconstruction, paths = _reconstruction(turn)
            isolated = _isolated(reconstruction)
            if not isolated:
                continue
            sid = str(session["session_id"])
            key = source_key, tid
            example = _recorded_example(turn, sid=sid, key=key, reconstruction=reconstruction,
                                        paths=paths, isolated=isolated)
            presentation = {name: value for name, value in example.items() if name != "session_id"}
            raw = _recorded_facts(turn)
            raw_hash = _digest(raw)
            if key in target_fingerprints:
                if (target_fingerprints[key] != _digest(presentation)
                        or any(record != raw_hash for record in target_records.get(key, []))):
                    raise EvidenceContextError("Conflicting target and Context copies of recorded Evidence")
                continue
            fingerprint = _digest({"presentation": presentation, "recorded_turn": raw})
            if key in seen:
                index, previous = seen[key]
                if previous != fingerprint:
                    raise EvidenceContextError("Conflicting copies of recorded Context")
                carriers = contexts[index]["carrier_session_ids"]
                if sid not in carriers:
                    carriers.append(sid)
                continue
            seen[key] = len(contexts), fingerprint
            # Context has no eligible EvidenceRef and is never an anchor source.
            example.pop("evidence_ref")
            example.update(usage="context_only", source_session_key=source_key,
                           context_trace_ref=refs[tid], carrier_session_ids=[sid],
                           recorded_turn=copy.deepcopy(dict(turn)))
            fields = (*_CONTENT_FIELDS, "recorded_turn")
            example["content_sha256"] = {field: hashlib.sha256(_content_bytes(example[field])).hexdigest()
                                         for field in fields}
            example["content_utf8_bytes"] = {field: len(_content_bytes(example[field])) for field in fields}
            contexts.append(example)
        if set(refs) - found:
            raise EvidenceContextError("Declared Context Trace is missing from the analysis snapshot")
    return contexts


def build_evidence_context(sessions, *, include_content=True, max_bytes=MAX_EVIDENCE_BYTES):
    """Present target Evidence without fabricating continuity, truth labels or IDs.

    ``example_id`` identifies a presentation entry, not a new Session or an
    EvidenceRef. Metadata-only applies to target bodies: verified Context bodies
    remain complete because writer raw Evidence does not carry them. Metadata-
    only and full projections share ``context_sha256``.
    No field is clipped; conflicting copies or an oversized packet fail before
    a consumer can mistake partial Evidence for complete Evidence.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise EvidenceContextError("Evidence context byte budget must be positive")
    sessions = list(sessions)
    examples, seen, duplicate_refs, anchor_sessions = [], {}, [], []
    expected_keys, source_sessions = set(), []
    for session in sessions:
        sid = str(session.get("session_id") or "")
        if not sid:
            raise EvidenceContextError("Evidence requires its recorded Session ID")
        if sid not in source_sessions:
            source_sessions.append(sid)
        provenance = session.get("provenance") or {}
        source_key = str(provenance.get("source_session_key") or sid)
        target_refs = _trace_refs(session, "target_trace_refs")
        found = set()
        for position, turn in enumerate(_target_turns(session)):
            tid = _trace_id(turn)
            if tid:
                found.add(tid)
            key = (source_key, tid or f"recorded-position:{position}")
            expected_keys.add(key)
            reconstruction, paths = _reconstruction(turn)
            isolated = _isolated(reconstruction)
            if isolated and sid not in anchor_sessions:
                anchor_sessions.append(sid)
            example = _recorded_example(turn, sid=sid, key=key, reconstruction=reconstruction,
                                        paths=paths, isolated=isolated)
            # Repeated projections of a source Trace cannot become additional evidence.
            fingerprint = _digest({key: value for key, value in example.items() if key != "session_id"})
            if key in seen:
                if seen[key] != fingerprint:
                    raise EvidenceContextError("Conflicting copies of the same recorded Evidence")
                duplicate_refs.append(tid or sid)
                continue
            seen[key] = fingerprint
            example["content_sha256"] = {field: hashlib.sha256(_content_bytes(example[field])).hexdigest()
                                         for field in _CONTENT_FIELDS}
            example["content_utf8_bytes"] = {field: len(_content_bytes(example[field])) for field in _CONTENT_FIELDS}
            examples.append(example)
        if target_refs - found:
            raise EvidenceContextError("Recorded target Trace is missing from the provided Evidence")
    packet = {
        "schema_version": "te.evidence-context.v1", "examples": examples,
        "anchor_required_for_session_ids": anchor_sessions,
        "coverage": {
            "expected_count": len(expected_keys), "included_count": len(examples),
            "source_session_ids": source_sessions, "omitted_evidence_refs": [],
            "duplicate_evidence_refs": list(dict.fromkeys(duplicate_refs)), "truncated_fields": [],
        },
    }
    context_examples = _context_records(sessions, seen) if anchor_sessions else []
    if context_examples:
        packet["context_examples"] = context_examples
        packet["coverage"].update(
            context_count=len(context_examples),
            context_trace_refs=[copy.deepcopy(example["context_trace_ref"]) for example in context_examples],
        )
    packet["coverage"]["context_sha256"] = _digest(packet)
    if not include_content:
        for example in packet["examples"]:
            for field in _CONTENT_FIELDS:
                example.pop(field)
    if len(_json(packet).encode()) > max_bytes:
        raise EvidenceContextTooLarge("Complete Evidence context exceeds the byte budget; no fields were truncated")
    return packet
