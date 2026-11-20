"""Public immutable MA runtime reads, injected into Replay by the composition root."""

import copy

from .replay_resources import effective_replay_config
from .repository import MARepository
from .schemas import digest


async def session_skill_names(services, tenant_id, connection_id, skill_ids):
    """Resolve immutable resource names only; never return current Skill versions."""
    if not connection_id or not skill_ids:
        return {}
    found = {}
    async with services.tx.tenant_tx(tenant_id) as conn:
        repo = MARepository(services.tx)
        for row in await repo.all_resources(conn, connection_id, kind="skill"):
            ident = row["record"].get("id")
            if ident in skill_ids:
                found.setdefault(ident, set()).add(row["logical_name"])
        missing = [ident for ident in skill_ids if ident not in found]
        if missing:
            rows = await conn.fetch(
                f"SELECT e.key AS name,e.value->>'id' AS id FROM {repo.schema}.ma_releases r "
                "CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(r.record->'skills')='object' "
                "THEN r.record->'skills' ELSE '{}'::jsonb END) e "
                "WHERE r.connection_id=$1 AND e.value->>'id'=ANY($2::text[])",
                connection_id, missing,
            )
            for row in rows:
                found.setdefault(row["id"], set()).add(row["name"])
    return {ident: next(iter(values)) for ident, values in found.items() if len(values) == 1}


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
        config = effective_replay_config(connection["config"], (discovered or {}).get("replay_resources"))
        # Runtime installation belongs to the recorded execution configuration.
        # Current settings provide credentials and isolated replay infrastructure.
        if recorded["config"].get("runtime_binding"):
            config["runtime_binding"] = copy.deepcopy(recorded["config"]["runtime_binding"])
        else:
            config.pop("runtime_binding", None)
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
            if not connection["enabled"]:
                continue
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


async def active_connections(services, tenant_id):
    """Connections eligible for new validation; paused history remains archived."""
    async with services.tx.tenant_tx(tenant_id) as conn:
        rows = await MARepository(services.tx).connections(conn)
    return {row["connection_id"] for row in rows if row["enabled"]}


async def automatic_fingerprints(services, tenant_id):
    """Current discovered execution identities, without exposing scripts or credentials."""
    from team_replay.fingerprints import execution_fingerprint
    result = {}
    async with services.tx.tenant_tx(tenant_id) as conn:
        repo = MARepository(services.tx)
        for row in await repo.connections(conn):
            if not row["enabled"] or row["status"] in {"drift", "paused"}:
                continue
            applied = await repo.applied(conn, row["connection_id"])
            record = (applied or {}).get("record") or {}
            fingerprint = execution_fingerprint({
                "agent_graph": record.get("agent_graph"), "environment": record.get("environment"),
                "connection_id": row["connection_id"],
            })
            if fingerprint:
                result[row["connection_id"]] = fingerprint
    return result
