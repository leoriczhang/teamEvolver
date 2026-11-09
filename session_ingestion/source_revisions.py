"""Factual upstream identity, lossless request provenance and revision planning.

These helpers never infer an actual model request from a conversation transcript.
An unchanged upstream unit is context, not another independent observation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .execution_observations import attach_observation

POLICY = "session-source.v1"
FACT_FIELDS = (
    "trace_id", "prompt_text", "response_text", "messages", "tool_calls", "tool_results",
    "tool_errors",
    "read_skills", "modified_skills", "used_skills", "injected_skills", "context_usage", "feedback",
    "system_prompt", "request_context", "source_artifacts", "output_artifacts", "runtime_context", "metrics",
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def trace_id(turn: dict) -> str:
    """Preserve mapper display identity separately from the upstream event ID."""
    return str(turn.get("source_trace_id") or turn.get("trace_id") or "")


def source_identity(adapter, metadata: dict, raw: dict, traces: list) -> dict:
    """Only explicit configuration or upstream project IDs establish scope."""
    endpoint = str(getattr(adapter, "host", "") or metadata.get("host") or "")
    url = urlsplit(endpoint)
    origin = urlunsplit((url.scheme.lower(), url.netloc.rsplit("@", 1)[-1].lower(), url.path.rstrip("/"), "", ""))
    instance = str(metadata.get("source_instance_id") or "")
    if not instance and origin and url.scheme in {"http", "https"} and url.hostname:
        instance = f"{metadata.get('provider') or type(adapter).__name__}:{origin}"
    declared = str(metadata.get("upstream_namespace") or metadata.get("project_id") or "")
    projects = {str(d.get("projectId") or d.get("project_id") or "") for d in [raw, *[
        item.get("trace", item) for item in traces if isinstance(item, dict)
    ]] if isinstance(d, dict)} - {""}
    conflict = len(projects) > 1 or bool(declared and projects and declared not in projects)
    namespace = declared or (next(iter(projects)) if len(projects) == 1 else "")
    return {"source_instance_id": instance, "upstream_namespace": namespace,
            "identity_verified": bool(instance and namespace and not conflict),
            "identity_evidence": "upstream_project" if projects else "declared_namespace",
            "identity_conflict": conflict}


def preserve_requests(session: dict, traces: list) -> dict:
    """Keep original trace and GENERATION inputs, distinguishing their strength."""
    by_id = {}
    for item in traces:
        if not isinstance(item, dict):
            continue
        trace = item.get("trace", item)
        if not isinstance(trace, dict):
            continue
        requests = []
        request_order = {}
        for observation in item.get("observations", trace.get("observations", [])) or []:
            if not isinstance(observation, dict) or str(observation.get("type", "")).upper() != "GENERATION":
                continue
            if "input" in observation:
                oid = str(observation.get("id") or "")
                request_order[oid] = str(observation.get("startTime") or observation.get("start_time") or "")
                requests.append({"observation_id": str(observation.get("id") or ""),
                                 "source_id": str(trace.get("id") or trace.get("trace_id") or ""),
                                 "model_call_id": str(observation.get("id") or ""),
                                 "evidence_level": "generation_input", "completeness": "unknown",
                                 "input": copy.deepcopy(observation["input"]),
                                 "model": observation.get("model"),
                                 "model_parameters": copy.deepcopy(observation.get("modelParameters") or {})})
        # Upstream APIs may enumerate observations in a different order on each
        # pull. Only observed chronology and stable IDs define request order.
        requests.sort(key=lambda value: (request_order[value["observation_id"]], value["observation_id"],
                                         digest(value)))
        context = {"evidence_level": "generation_input" if requests else "trace_input",
                   "completeness": "unknown", "generation_requests": requests}
        if "input" in trace:
            context["trace_input"] = copy.deepcopy(trace["input"])
        attach_observation(context, trace)
        if requests or "input" in trace or "execution_binding" in context or "execution_observation_errors" in context:
            by_id[str(trace.get("id") or trace.get("trace_id") or "")] = (
                context, trace.get("updatedAt") or trace.get("updated_at") or "", trace.get("timestamp") or "",
            )
    for turn in session.get("turns") or []:
        saved = by_id.get(trace_id(turn))
        if saved:
            turn["request_context"], turn["source_updated_at"], turn["source_timestamp"] = saved
        elif "request_context" not in turn:
            turn["request_context"] = {"evidence_level": "conversation_only", "completeness": "unknown"}
    runtime_traces = [item.get("trace", item) for item in traces if isinstance(item, dict)]
    runtime_metadata = [item["metadata"] if isinstance(item, dict) and isinstance(item.get("metadata"), dict)
                        else {} for item in runtime_traces]
    if runtime_metadata and all(item.get("runtime_type") == "agentscope-java" for item in runtime_metadata):
        agents = {item.get("agent_id") for item in runtime_metadata}
        if len(agents) == 1 and next(iter(agents)):
            session["runtime"] = {"type": "agentscope-java", "version": "2.0.4"}
            session["runtime_context"] = {**session.get("runtime_context", {}), "agent_id": next(iter(agents))}
    return session


def reported_session(session: dict) -> dict:
    """Keep Agent reports without granting server-attested source privileges."""
    result = copy.deepcopy(session)
    reported = {key: result.pop(key) for key in ("provenance", "source_identity", "analysis_context") if key in result}
    meta = result.get("meta") or {}
    reserved = ("source_session_key", "snapshot_revision", "target_trace_refs", "context_trace_refs",
                "identity_verified", "trace_identity_verified", "revision_conflicts")
    claimed_meta = {key: meta.pop(key) for key in reserved if key in meta}
    if claimed_meta:
        reported["meta"] = claimed_meta
    if reported:
        result["reported_provenance"] = reported
    result["provenance"] = {"schema_version": POLICY, "identity_verified": False,
                            "reason": "Agent-reported source identity is not server-attested"}
    for turn in result.get("turns") or []:
        if "request_context" in turn:
            original = copy.deepcopy(turn["request_context"])
            turn["reported_request_context"] = original
            turn["request_context"] = {"evidence_level": "agent_reported", "completeness": "unknown",
                                       "reported": original}
    return result


def trace_revision(turn: dict) -> str:
    facts = {k: turn[k] for k in FACT_FIELDS if k in turn}
    if trace_id(turn):
        facts["trace_id"] = trace_id(turn)
    return digest(facts)


def _time(turn):
    try:
        return datetime.fromisoformat(str(turn.get("source_updated_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def plan_snapshot(session: dict, *, tenant_id: str, external_id: str, previous: dict | None = None) -> dict:
    """Merge partial observations without interpreting missing units as deletion."""
    incoming = copy.deepcopy(session)
    identity = incoming.get("source_identity") or {}
    if not identity.get("identity_verified"):
        incoming["provenance"] = {"schema_version": POLICY, "identity_verified": False,
                                  "reason": "upstream namespace or source instance is unverified"}
        return incoming
    key = "src_" + digest([tenant_id, identity["source_instance_id"], identity["upstream_namespace"], external_id])
    previous = previous or {}
    incoming = {**copy.deepcopy(previous), **incoming}
    old = {trace_id(t): copy.deepcopy(t) for t in previous.get("turns") or [] if trace_id(t)}
    order = list(old)
    targets, conflicts, stale = set(), [], []
    turns = incoming.get("turns") or []
    reliable = bool(turns) and all(trace_id(t) for t in turns)
    if reliable:
        for turn in turns:
            tid = trace_id(turn)
            prior = old.get(tid)
            if prior and trace_revision(prior) != trace_revision(turn):
                before, after = _time(prior), _time(turn)
                if before and after and after < before:
                    stale.append(tid)
                    continue
                if not before or not after or before == after:
                    conflicts.append(tid)
            if not prior or trace_revision(prior) != trace_revision(turn):
                targets.add(tid)
            if tid not in old:
                order.append(tid)
            old[tid] = copy.deepcopy(turn)
        if all(old[tid].get("source_timestamp") for tid in order):
            order.sort(key=lambda tid: (old[tid]["source_timestamp"], tid))
        incoming["turns"] = [old[tid] for tid in order]
        if any("messages" in t for t in incoming["turns"]):
            incoming["messages"] = [message for turn in incoming["turns"] for message in turn.get("messages") or []]
    else:
        # Without stable unit IDs, only exact snapshot equality is safe to dedupe.
        targets = {trace_id(t) or f"unverified:{i}" for i, t in enumerate(turns)}
    refs = []
    prefix = []
    for i, turn in enumerate(incoming.get("turns") or []):
        tid = trace_id(turn) or f"unverified:{i}"
        revision = trace_revision(turn)
        prefix.append([tid, revision])
        refs.append({"source_session_key": key, "trace_id": tid, "revision": revision,
                     "context_revision": digest(prefix)})
    old_contexts = {r["trace_id"]: r.get("context_revision")
                    for r in (previous.get("provenance") or {}).get("trace_revisions") or []}
    targets.update(r["trace_id"] for r in refs
                   if r["trace_id"] in old_contexts and old_contexts[r["trace_id"]] != r["context_revision"])
    factual = {"turns": [[r["trace_id"], r["revision"]] for r in refs],
               "system_prompt": incoming.get("system_prompt"), "runtime": incoming.get("runtime"),
               "runtime_context": incoming.get("runtime_context")}
    revision = digest(factual)
    # A global context change affects every unit, even when its own text is unchanged.
    if previous and any(incoming.get(k) != previous.get(k) for k in ("system_prompt", "runtime_context", "runtime")):
        targets.update(r["trace_id"] for r in refs)
    incoming["provenance"] = {
        "schema_version": POLICY, **identity, "source_session_key": key, "upstream_session_id": external_id,
        "snapshot_revision": revision, "trace_identity_verified": reliable, "trace_revisions": refs,
        "target_trace_refs": [r for r in refs if r["trace_id"] in targets],
        "context_trace_refs": [r for r in refs if r["trace_id"] not in targets],
        "revision_conflicts": conflicts, "ignored_stale_trace_ids": stale,
    }
    incoming["meta"] = {**(incoming.get("meta") or {}), "source_session_key": key,
                        "snapshot_revision": revision}
    return incoming


def segment_provenance(session: dict, turns: list[dict]) -> dict:
    provenance = copy.deepcopy(session.get("provenance") or {})
    ids = {trace_id(t) for t in turns}
    for name in ("trace_revisions", "target_trace_refs"):
        if name in provenance:
            provenance[name] = [r for r in provenance[name] if r["trace_id"] in ids]
    targets = {r["trace_id"] for r in provenance.get("target_trace_refs") or []}
    if session.get("provenance", {}).get("trace_revisions"):
        provenance["context_trace_refs"] = [r for r in session["provenance"]["trace_revisions"]
                                            if r["trace_id"] not in targets]
    return provenance
