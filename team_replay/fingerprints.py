"""Recorded execution provenance; runtime drift does not invalidate Evidence."""
import copy
from urllib.parse import urlsplit, urlunsplit

from ._util import stable_hash


def execution_fingerprint(runtime):
    graph = runtime.get("agent_graph") or {}
    agents = {
        ident: {k: (
            [{field: value for field, value in skill.items() if field != "display_name"}
             for skill in v or []] if k == "skills" else v
        ) for k, v in agent.items() if k not in {
            "created_at", "updated_at", "version", "name", "display_name",
        }}
        for ident, agent in graph.get("agents", {}).items()
    }
    environment = runtime.get("environment") or {}
    return {"agent_hash": stable_hash(agents), "environment_hash": stable_hash(environment.get("config")),
            "connection_id": runtime.get("connection_id", "")} if agents and environment.get("config") else None


def source_runtime(item):
    provenance = item.get("provenance") or {}
    return (item.get("runtime") or provenance.get("source_runtime")
            or (provenance.get("session_snapshot") or {}).get("runtime") or {})


def execution_provenance(item):
    """Small public record; exact configs remain in the immutable Session snapshot."""
    if item.get("execution_provenance"):
        return copy.deepcopy(item["execution_provenance"])
    runtime = source_runtime(item)
    if not runtime:
        return {}
    agents = []
    for ident, agent in (runtime.get("agent_graph") or {}).get("agents", {}).items():
        model = agent.get("model") or {}
        public_model = {k: model[k] for k in ("id", "name", "provider", "api_type") if k in model}
        for key in ("base_url", "endpoint"):
            if model.get(key):
                url = urlsplit(model[key])
                public_model[key] = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        agents.append({
            "id": agent.get("id", ident), "version": agent.get("version"), "model": public_model,
            "skills": [{k: s[k] for k in ("id", "skill_id", "name", "type", "version") if k in s}
                       for s in agent.get("skills") or []],
        })
    environment = runtime.get("environment") or {}
    return {
        "session_id": item.get("session_id"), "content_hash": item.get("content_hash"),
        **{k: (v.isoformat() if hasattr(v, "isoformat") else v)
           for k in ("business_time", "ingested_at") if (v := item.get(k)) is not None},
        "type": runtime.get("type"), "connection_id": runtime.get("connection_id"),
        "connection_revision": runtime.get("connection_revision"),
        "entry_agent_id": (runtime.get("agent_graph") or {}).get("entry_agent_id"),
        "agents": agents, "environment": {k: environment[k] for k in ("id", "name") if k in environment},
        "execution_fingerprint": item.get("execution_fingerprint") or execution_fingerprint(runtime),
    }


def ineligibility_reason(item, current):
    runtime = source_runtime(item)
    connection_id = runtime.get("connection_id") or (item.get("runtime_context") or {}).get("connection_id")
    if runtime.get("type") != "managed-agents" and not connection_id:
        return ""
    if connection_id not in current:
        return "Source connection is unavailable or disabled"
    if runtime.get("snapshot_missing"):
        return "Historical execution snapshot is incomplete"
    recorded = item.get("execution_fingerprint") or execution_fingerprint(runtime)
    if not recorded:
        return "Historical execution fingerprint is incomplete"
    return ""
