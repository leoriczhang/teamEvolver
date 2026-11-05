"""Public immutable MA runtime reads, injected into Replay by the composition root."""

import copy

from .repository import MARepository
from .schemas import digest


async def freeze(services, tenant_id, cases, skill_name=""):
    """Freeze recorded execution facts, never replace them with latest Agents."""
    repo, fixed = MARepository(services.tx), {}
    for case in cases:
        source = case.get("provenance") or {}
        runtime = source.get("source_runtime") or (source.get("session_snapshot") or {}).get("runtime") or {}
        if runtime.get("type") != "managed-agents":
            continue
        ident = runtime.get("connection_id")
        async with services.tx.tenant_tx(tenant_id) as conn:
            recorded = await repo.connection(conn, ident, runtime.get("connection_revision"))
            if not recorded:
                raise ValueError("Recorded MA connection revision is unavailable")
            # Execution facts stay historical. Replay infrastructure and target
            # bindings freeze the administrator's configuration at acceptance.
            connection = await repo.connection(conn, ident)
            targets = await repo.all_resources(conn, ident, kind="skill")
            discovered = await repo.discovery_snapshot(conn, ident, connection["revision"])
        graph = runtime.get("agent_graph")
        if not graph or runtime.get("snapshot_missing") or runtime.get("materials_missing"):
            raise ValueError("Recorded MA configuration or input materials are incomplete")
        config = copy.deepcopy(connection["config"])
        if not config["replay_enabled"] or not connection["enabled"]:
            raise ValueError("MA True Replay is disabled or connection is paused")
        replay_environment = (discovered or {}).get("replay_environment")
        if not replay_environment:
            raise ValueError("Discover the configured Replay Environment before validation")
        members = next((r["record"].get("agents", []) for r in targets
                        if r["logical_name"] == skill_name), []) if skill_name else []
        if skill_name and not members:
            raise ValueError("Choose target MA members before validation")
        if not set(members) <= set(graph["agents"]):
            raise ValueError("Target MA members are absent from the recorded Agent graph")
        record = {"connection_id": ident, "connection_revision": connection["revision"],
                  "config": config, "config_hash": connection["config_hash"], "target_skill_name": skill_name,
                  "agent_graph": copy.deepcopy(graph),
                  "environment": copy.deepcopy(runtime.get("environment") or {}),
                  "replay_environment": copy.deepcopy(replay_environment),
                  "skill_bindings": {r["logical_name"]: copy.deepcopy(r["record"]) for r in targets},
                  "target_agents": members, "binding_hash": digest(sorted(members))}
        record["runtime_hash"] = digest({"agent_graph": record["agent_graph"],
                                         "environment": record["environment"].get("config"),
                                         "target_agents": sorted(members)})
        fixed[str(case["case_id"])] = record
    return fixed


async def required_targets(services, tenant_id, skill_name):
    repo, targets = MARepository(services.tx), []
    async with services.tx.tenant_tx(tenant_id) as conn:
        for connection in await repo.connections(conn):
            resources = await repo.all_resources(conn, connection["connection_id"], kind="skill")
            if any(r["logical_name"] == skill_name and r["record"].get("agents") for r in resources):
                members = next(r["record"]["agents"] for r in resources if r["logical_name"] == skill_name)
                applied = await repo.applied(conn, connection["connection_id"])
                snapshot = (applied or {}).get("record") or {}
                environment = (snapshot.get("environment") or {}).get("config")
                targets.append({"connection_id": connection["connection_id"],
                                "connection_revision": connection["revision"],
                                "config_hash": connection["config_hash"], "binding_hash": digest(sorted(members)),
                                "runtime_hash": digest({"agent_graph": snapshot.get("agent_graph"),
                                                        "environment": environment,
                                                        "target_agents": sorted(members)})})
    return targets
