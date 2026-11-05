"""Pinned Agent graphs and safe custom Skill takeover."""

from __future__ import annotations

import asyncio
import io
import stat
import zipfile

from team_skills.library.bundle import bundle_tree_sha256
from team_skills.transfer.packages import discover, make_zip, read_zip

from .repository import MARepository
from .schemas import digest


async def agent_graph(client, entry_agent_id, version=None, *, overrides=None):
    graph, visiting = {}, set()

    async def visit(ident, pinned=None, override=None):
        if ident in visiting:
            raise ValueError("Cyclic MA membership is unsupported")
        if ident in graph:
            if pinned is not None and str(graph[ident]["version"]) != str(pinned):
                raise ValueError("Same Agent is referenced with ambiguous versions")
            return
        value = await client.request("GET", f"agents/{ident}",
                                     params={"version": pinned} if pinned is not None else {})
        if override:
            value.update(override)
        if str(value.get("id")) != ident or value.get("version") is None:
            raise ValueError("MA Agent snapshot missing ID or version")
        members = (value.get("multiagent") or {}).get("agents") or []
        if len(members) > 20 or len(value.get("skills") or []) > 50:
            raise ValueError("MA member or Skill limit exceeded")
        visiting.add(ident)
        for member in members:
            if member.get("type") == "self":
                continue
            if not member.get("id") or member.get("version") is None:
                raise ValueError("MA member must pin an ID and version")
            await visit(member["id"], member["version"])
        visiting.remove(ident)
        graph[ident] = value

    await visit(entry_agent_id, version, overrides)
    return {"entry_agent_id": entry_agent_id, "agents": graph}


def graph_postorder(graph):
    """Recompute order after JSON persistence (canonical storage sorts keys)."""
    ordered, visiting, visited = [], set(), set()

    def visit(ident):
        if ident in visiting:
            raise ValueError("Cyclic MA graph")
        if ident in visited:
            return
        visiting.add(ident)
        for child in (graph["agents"][ident].get("multiagent") or {}).get("agents") or []:
            if child.get("type") != "self":
                visit(child["id"])
        visiting.remove(ident)
        visited.add(ident)
        ordered.append(ident)
    visit(graph["entry_agent_id"])
    return ordered


def read_skill_zip(data):
    if len(data) > 30 * 1024 * 1024:
        raise ValueError("MA ZIP exceeds 30 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if sum(not e.is_dir() for e in entries) > 500 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
            raise ValueError("MA package exceeds 500 files or 64 MiB expanded bytes")
        if any(stat.S_ISLNK(e.external_attr >> 16) for e in entries):
            raise ValueError("MA package contains a symbolic link")
    files = read_zip(data)
    roots = {path.split("/", 1)[0] for path in files}
    if len(roots) != 1 or sum(path.endswith("/SKILL.md") for path in files) != 1:
        raise ValueError("MA package must have one top-level Skill and one SKILL.md")
    packages = discover(files)
    if len(packages) != 1:
        raise ValueError("MA package contains multiple Skills")
    return packages[0]


def skill_archive(item):
    if len(item.files) > 500:
        raise ValueError("MA Skill file limit exceeded")
    result = make_zip([item])
    if len(result) > 30 * 1024 * 1024:
        raise ValueError("MA Skill compressed limit exceeded")
    return result


async def discover_connection(task, services, client, connection, operation):
    repo, config = MARepository(services.tx), connection["config"]
    checkpoint = operation["checkpoint"]
    graph = checkpoint.get("graph")
    if not graph:
        graph = await agent_graph(client, config["entry_agent_id"])
        environment = await client.request("GET", f"environments/{config['environment_id']}")
        replay_environment = (await client.request("GET", f"environments/{config['replay_environment_id']}")
                              if config["replay_enabled"] else None)
        checkpoint = {"graph": graph, "environment": environment, "replay_environment": replay_environment,
                      "skills": {}}
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.checkpoint(conn, task.task_id, checkpoint)
    references = {}
    for ident, agent in graph["agents"].items():
        for ref in agent.get("skills") or []:
            if ref.get("type") != "custom":
                continue
            key = str(ref["skill_id"])
            pinned = str(ref["version"])
            existing = references.setdefault(key, {"version": pinned, "agents": []})
            if existing["version"] != pinned:
                raise ValueError("Custom Skill is used with multiple versions; resolve before takeover")
            existing["agents"].append(ident)
    waiting = []
    for ident, ref in references.items():
        item = checkpoint["skills"].get(ident)
        if not item:
            data = await client.download(f"skills/{ident}/versions/{ref['version']}/content")
            package = await asyncio.to_thread(read_skill_zip, data)
            tree = bundle_tree_sha256(package.files)
            # The public import command pins complete bytes into existing transfer storage.
            accepted = await services.extras["ma_import_bundle"](
                task, name=package.name, archive=data, tree_sha256=tree,
                command_id="ma-import:" + digest([task.tenant_id, task.subject_id, ident, ref["version"]])[:40],
            )
            item = {"id": ident, "name": package.name, "version": ref["version"], "source_version": ref["version"],
                    "tree_sha256": tree, "agents": ref["agents"], "transfer_id": accepted.get("command_id"),
                    "team_version": accepted.get("version"), "status": "importing"}
            checkpoint["skills"][ident] = item
        if not item.get("team_version"):
            result = await services.extras["ma_import_result"](task.tenant_id, item["transfer_id"], item["name"],
                                                              item["tree_sha256"])
            if result.get("failed"):
                raise ValueError("Skill import failed: " + item["name"])
            item["team_version"] = result.get("version")
        if not item["team_version"]:
            waiting.append(item["name"])
        else:
            item["status"] = "managed"
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.checkpoint(conn, task.task_id, checkpoint)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        for ident, agent in graph["agents"].items():
            await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id, kind="agent",
                            name=ident, record={"id": ident, "version": str(agent["version"]),
                                                "name": agent.get("name", ident), "snapshot": agent})
        for item in checkpoint["skills"].values():
            await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                            kind="skill", name=item["name"], record=item)
        if waiting:
            await repo.status(conn, task.subject_id, "importing", {"waiting": waiting})
            return {"waiting": True}
        skills = {s["name"]: {k: v for k, v in s.items() if k not in {"transfer_id", "status"}}
                  for s in checkpoint["skills"].values()}
        manifest_hash = digest([(n, s["team_version"], s["tree_sha256"]) for n, s in sorted(skills.items())])
        initial_id = "ma-initial:" + digest([connection["config_hash"], graph, skills])[:40]
        existing = await repo.applied(conn, task.subject_id)
        if existing is None:
            record = {"entry_agent_id": config["entry_agent_id"],
                      "entry_agent_version": str(graph["agents"][config["entry_agent_id"]]["version"]),
                      "agents": {i: {"id": i, "version": str(a["version"]), "name": a.get("name")}
                                 for i, a in graph["agents"].items()},
                      "skills": skills, "environment_id": config["environment_id"],
                      "environment_hash": digest(checkpoint["environment"]["config"]),
                      "environment": checkpoint["environment"], "agent_graph": graph,
                      "connection_revision": connection["revision"], "release_id": initial_id,
                      "manifest_hash": manifest_hash}
            await repo.save_release(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                    release_id=initial_id, manifest_hash=manifest_hash, record=record, status="applied")
        await repo.status(conn, task.subject_id, "ready", {
            "agent_count": len(graph["agents"]), "managed_skill_count": len(skills),
            "replay_capability": replay_capability(config, graph, checkpoint.get("replay_environment")),
        }, synced=True)
        result = {"agent_count": len(graph["agents"]), "managed_skill_count": len(skills), "release_id": initial_id}
        await repo.checkpoint(conn, task.task_id, checkpoint, result)
    return result


def replay_capability(config, graph, environment):
    if not config.get("replay_enabled"):
        return {"supported": False, "reason": "disabled"}
    if not environment or environment.get("config", {}).get("type") != "cloud":
        return {"supported": False, "reason": "isolated cloud replay Environment required"}
    isolation = config.get("replay_isolation") or {}
    for agent in graph["agents"].values():
        if agent.get("memory") or agent.get("memory_store_id"):
            return {"supported": False, "reason": "persistent Memory snapshot unavailable"}
        if agent.get("mcp_servers") and not isolation.get("mcp"):
            return {"supported": False, "reason": "MCP isolation not configured"}
        if any(t.get("type") == "custom" for t in agent.get("tools", [])) and not isolation.get("custom_tools"):
            return {"supported": False, "reason": "custom tool host isolation not configured"}
    if config.get("replay_vault_ids") and not isolation.get("vaults"):
        return {"supported": False, "reason": "Replay Vault isolation must be explicitly enabled"}
    return {"supported": True}
