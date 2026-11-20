"""Fenced cloud Skill edits, native versions, deployment and durable receipts."""
from __future__ import annotations

from team_skills.library.bundle import bundle_tree_sha256, decode_bundle_payload

from .discovery import agent_graph, graph_postorder, skill_archive
from .effects import Effects
from .repository import MARepository
from .schemas import digest
from .synchronization import update_agent


async def write_skill(task, services, client, connection, operation):
    provider, repo = services.extras["cloud_skill_provider"], MARepository(services.tx)
    request = operation["input"]["request"]
    name, ident, base = request["name"], request["skill_id"], request["base_version"]
    effect_id = task.task_id + ":skill"
    effects = Effects(services, task.tenant_id, task.subject_id, task.task_id, task=task)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        prior_effect = await repo.effect(conn, effect_id)
        validator = services.extras.get("cloud_candidate_guard")
        if validator:
            await validator(conn, task, request)
    # A confirmed upload can be resumed even though it already advanced the head.
    if not prior_effect or prior_effect["status"] == "failed":
        if ident:
            remote = await client.request("GET", f"skills/{ident}")
            latest = int(remote.get("latest_version") or 0)
            if latest != base and not (request["action"] == "delete" and latest == 0):
                raise ValueError("Cloud Skill changed after acceptance; reload before publishing")
        elif base:
            raise ValueError("New cloud Skill cannot have an existing base version")
    action = request["action"]
    if request.get("no_change"):
        meta, bundle = await provider.fetch_version(task.tenant_id, connection, client, ident, name, base)
        if meta["tree_sha256"] != bundle_tree_sha256(decode_bundle_payload(request["bundle"])):
            raise ValueError("Cloud unchanged package readback mismatch")
        result = {
            "name": name, "skill_id": ident, "version": base, "previous_version": base,
            "action": action, "status": "unchanged", "skill_backend": "managed-agents",
            "mutation_id": request["mutation_id"], "files": sorted(bundle), "written": [],
            "cloud": {"synced": True}, "readback_verified": True,
        }
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            if request["metadata"].get("candidate_id"):
                await services.extras["cloud_skill_committed"](
                    conn, task, request, result, {"latest_version": str(base)},
                )
            await repo.checkpoint(conn, task.task_id, {}, result)
        return result
    if action == "delete":
        async def remove():
            from teamEvolver.managed_agents import MAError
            versions = await client.listing(f"skills/{ident}/versions")
            if any(int(v["version"]) > base for v in versions):
                raise ValueError("Cloud Skill advanced during deletion; review the new version")
            # MA rejects Skill deletion while versions exist. Preserve checked
            # historical bytes before removing each owned version by native ID.
            for item in sorted(versions, key=lambda v: int(v["version"])):
                native = int(item["version"])
                await provider.fetch_version(task.tenant_id, connection, client, ident, name, native)

                async def drop(native=native):
                    await client.request("DELETE", f"skills/{ident}/versions/{native}")
                    return {"id": ident, "version": str(native), "deleted": True}

                async def gone(row, native=native):
                    try:
                        await client.request("GET", f"skills/{ident}/versions/{native}")
                    except MAError as exc:
                        if exc.status == 404:
                            return {"id": ident, "version": str(native), "deleted": True}
                        raise
                    return None
                await effects.apply(effect_id + f":version:{native}", "cloud-skill-version-delete",
                                    {"skill_id": ident, "version": native}, drop, gone)
            await client.request("DELETE", f"skills/{ident}")
            try:
                await client.request("GET", f"skills/{ident}")
            except MAError as exc:
                if exc.status != 404:
                    raise
            else:
                raise ValueError("Deleted cloud Skill remains readable")
            return {"id": ident, "deleted": True}
        async def removed(row):
            from teamEvolver.managed_agents import MAError
            try:
                await client.request("GET", f"skills/{ident}")
            except MAError as exc:
                if exc.status == 404:
                    return {"id": ident, "deleted": True}
                raise
            # DELETE targets the same resource ID and is idempotent. A readable
            # resource permits resuming its staged version cleanup after a
            # partial deletion, while each version has its own durable receipt.
            return await remove()
        version, tree = base, ""
    else:
        if action == "rollback":
            _, bundle = await provider.fetch_version(
                task.tenant_id, connection, client, ident, name, request["target_version"],
            )
        else:
            bundle = decode_bundle_payload(request["bundle"])
        from team_skills.transfer.packages import package
        archive = skill_archive(package(name, bundle))
        tree = bundle_tree_sha256(bundle)
        title = "te-" + digest([task.tenant_id, task.task_id])[:32]

        async def recover(row):
            sid = ident
            if not sid:
                matches = [s for s in await client.listing("skills", source="custom")
                           if s.get("display_title") == title and s.get("name") == name]
                if len(matches) != 1:
                    return None
                sid = matches[0]["id"]
            matches = []
            for item in await client.listing(f"skills/{sid}/versions"):
                native = int(item["version"])
                if native <= base:
                    continue
                meta, _ = await provider.fetch_version(task.tenant_id, connection, client, sid, name, native)
                if meta["tree_sha256"] == tree:
                    matches.append({"id": sid, "version": str(native)})
            return matches[0] if len(matches) == 1 else None

        async def upload():
            result = await client.upload_skill(name, archive, skill_id=ident,
                                               display_title=title if not ident else name)
            return {**result, "id": ident or result.get("skill_id") or result["id"],
                    "version": str(result.get("version") or result["latest_version"])}

        receipt = await effects.apply(
            effect_id, "cloud-skill-version",
            {"skill_id": ident, "name": name, "base_version": base, "tree_sha256": tree, "title": title},
            upload, recover,
        )
        ident, version = receipt["id"], int(receipt["version"])
        meta, _ = await provider.fetch_version(task.tenant_id, connection, client, ident, name, version)
        remote = await client.request("GET", f"skills/{ident}")
        # MA assigns max+1 but does not expose upload CAS. A raced version stays
        # in history and is never falsely reported/deployed as a guarded publish.
        if version != base + 1 or int(remote["latest_version"]) != version:
            raise ValueError("Concurrent cloud Skill update; uploaded version retained, deployment paused")
        if meta["tree_sha256"] != tree:
            raise ValueError("Cloud Skill file tree readback mismatch")

    graph = await agent_graph(client, connection["config"]["entry_agent_id"])
    targets = set(request["agents"])
    if not targets <= set(graph["agents"]):
        raise ValueError("Selected MA members are absent from the current Agent graph")
    # Merge against actual live references, preserving unrelated external edits.
    versions, affected = {}, set(targets)
    for member in graph_postorder(graph):
        current = graph["agents"][member]
        refs = [r for r in current.get("skills") or [] if r.get("skill_id") == ident]
        bound = str(refs[0]["version"]) if refs else str(version)
        if refs:
            affected.add(member)
        children = (current.get("multiagent") or {}).get("agents") or []
        if any(c.get("id") in affected for c in children):
            affected.add(member)
        expected = {name: {"id": ident, "version": bound, "agents": [member] if refs else []}}
        desired = {name: {"id": ident, "version": str(version), "agents": sorted(targets)}}
        value = (await update_agent(
            client, effects, member, desired, desired if action != "delete" else {}, versions,
            {name} if action == "delete" else set(), task.task_id, expected,
        ) if member in affected else current)
        versions[member] = {"id": member, "version": str(value["version"]), "name": value.get("name")}
    readback = await agent_graph(client, connection["config"]["entry_agent_id"])
    for member, agent in readback["agents"].items():
        refs = [r for r in agent.get("skills") or [] if r.get("skill_id") == ident]
        wanted = action != "delete" and member in targets
        if (wanted and not any(str(r["version"]) == str(version) for r in refs)) or (not wanted and refs):
            raise ValueError("Cloud Skill Agent reference readback mismatch")
    if action == "delete":
        # MA may reject deletion while attached. Detach and verify the graph
        # first; durable Agent receipts make this order safe to resume.
        receipt = await effects.apply(effect_id, "cloud-skill-delete",
                                      {"skill_id": ident, "base_version": base}, remove, removed)
    else:
        remote = await client.request("GET", f"skills/{ident}")
        if int(remote["latest_version"]) != version:
            raise ValueError("Cloud Skill advanced during deployment; publication requires review")
    result = {
        "name": name, "skill_id": ident, "version": version, "previous_version": base,
        "action": action, "status": "published", "skill_backend": "managed-agents",
        "mutation_id": request["mutation_id"], "deleted": action == "delete",
        "restored_from": request["target_version"], "files": sorted(bundle) if action != "delete" else [],
        **({"new_version": version} if action == "rollback" else {}),
        "written": sorted(bundle) if action != "delete" else [],
        "cloud": {"synced": True}, "readback_verified": True,
    }
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        old = next((r["record"] for r in await repo.all_resources(conn, task.subject_id, kind="skill")
                    if r["logical_name"] == name), {})
        binding = {
            **old, "id": ident, "name": name, "version": str(version), "source_version": str(version),
            "latest_version": str(version), "bound_version": str(version) if targets else None,
            "bound_versions": {m: str(version) for m in targets}, "agents": sorted(targets),
            "tree_sha256": tree, "skill_backend": "managed-agents", "deleted": action == "delete",
        }
        finalize = services.extras.get("cloud_skill_committed")
        if finalize:
            await finalize(conn, task, request, result, binding)
        await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                        kind="skill", name=name, record=binding)
        for member, value in readback["agents"].items():
            await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id, kind="agent", name=member,
                            record={"id": member, "version": str(value["version"]),
                                    "name": value.get("name"), "snapshot": value})
        await repo.checkpoint(conn, task.task_id, {"receipt": receipt}, result)
    # Reconciliation observes the cloud and records the full graph/environment;
    # it never uploads a TE library copy.
    from .commands import enqueue
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await enqueue(conn, services, tenant_id=task.tenant_id, connection_id=task.subject_id,
                      kind="ma.reconcile", operation_id=task.task_id + ":observe")
    return result
