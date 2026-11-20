"""Execution identity used to keep obsolete automatic inputs historical."""
from ._util import stable_hash


def execution_fingerprint(runtime):
    graph = runtime.get("agent_graph") or {}
    agents = {
        ident: {k: v for k, v in agent.items() if k not in {
            "created_at", "updated_at", "version", "name", "display_name",
        }}
        for ident, agent in graph.get("agents", {}).items()
    }
    environment = runtime.get("environment") or {}
    return {"agent_hash": stable_hash(agents), "environment_hash": stable_hash(environment.get("config")),
            "connection_id": runtime.get("connection_id", "")} if agents and environment.get("config") else None


def ineligibility_reason(item, current):
    runtime = item.get("runtime") or (item.get("provenance") or {}).get("source_runtime") or {}
    connection_id = runtime.get("connection_id") or (item.get("runtime_context") or {}).get("connection_id")
    if runtime.get("type") != "managed-agents" and not connection_id:
        return ""
    expected = current.get(connection_id)
    if not expected:
        return "Source connection or current execution fingerprint is unavailable"
    recorded = item.get("execution_fingerprint") or execution_fingerprint(runtime)
    if not recorded:
        return "Historical execution fingerprint is incomplete"
    if recorded != expected:
        return "Agent, tools, Environment or Skill binding changed"
    return ""
