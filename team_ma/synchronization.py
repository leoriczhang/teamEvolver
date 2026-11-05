"""Pull published team Skills, apply leaf-to-root MA versions and verify a release."""

from __future__ import annotations

import base64
import copy
import hashlib
import json

import httpx

from team_skills.library.bundle import bundle_tree_sha256
from team_skills.transfer.packages import package
from teamEvolver.managed_agents import MAError

from .discovery import agent_graph, graph_postorder, read_skill_zip, skill_archive
from .effects import Effects
from .recovery import delete_owned, recover_owned
from .repository import MARepository
from .runtime_binding import bind_runtime
from .schemas import digest


def writable_agent(value):
    return {k: copy.deepcopy(v) for k, v in value.items()
            if k not in {"id", "type", "created_at", "updated_at", "overridden_fields", "scene"}}


async def pull(services, tenant_id, config, etag=""):
    async with services.tx.tenant_tx(tenant_id) as conn:
        token = await services.extras["secret_repo"].resolve(conn, secret_ref=config["tenant_token_ref"])
    factory = services.extras.get("ma_pull_client") or httpx.AsyncClient
    async with factory(timeout=60, follow_redirects=False) as client:
        async with client.stream(
            "GET", config["team_evolver_url"].rstrip("/") + "/sync/skills",
            params={"user_id": config["default_user_id"]},
            headers={"Authorization": "Bearer " + token, **({"If-None-Match": etag} if etag else {})},
        ) as response:
            if response.status_code == 304:
                return None, etag
            if response.status_code != 200:
                raise ValueError(f"Team Skill pull failed (HTTP {response.status_code})")
            from teamEvolver.managed_agents.client import _bounded
            data = await _bounded(response, 96 * 1024 * 1024)
            manifest = json.loads(data)
            if manifest.get("status") != "ok" or not isinstance(manifest.get("skills"), list):
                raise ValueError("Team Skill pull did not return a complete manifest")
            if manifest.get("total") != len(manifest["skills"]):
                raise ValueError("Team Skill pull manifest count mismatch")
            return manifest, response.headers.get("etag", "")


def decode_manifest(manifest):
    output = {}
    for skill in manifest["skills"]:
        name = skill["name"]
        if name in output:
            raise ValueError("Duplicate Skill in pull manifest")
        files = {}
        for entry in skill["files"]:
            if entry["path"] in files:
                raise ValueError("Duplicate file in pull Bundle")
            files[entry["path"]] = base64.b64decode(entry["content_b64"], validate=True)
        item = package(name, files)
        tree = bundle_tree_sha256(item.files)
        if tree != skill.get("tree_sha256"):
            raise ValueError("Published Skill tree hash mismatch")
        skill_archive(item)  # enforce remote limits before any external POST
        output[name] = {**skill, "package": item}
    return output


async def find_skill_version(client, skill_id, tree):
    for version in await client.listing(f"skills/{skill_id}/versions", max_pages=20):
        data = await client.download(f"skills/{skill_id}/versions/{version['version']}/content")
        item = read_skill_zip(data)
        if bundle_tree_sha256(item.files) == tree:
            return {**version, "id": skill_id, "version": str(version["version"])}
    return None


async def update_agent(client, effects, ident, expected, skill_versions, child_versions, removed, release_id,
                       previous_bindings=None):
    for attempt in range(3):
        current = await client.request("GET", f"agents/{ident}")
        current_skills = copy.deepcopy(current.get("skills") or [])
        for name, binding in (previous_bindings or expected).items():
            if ident not in binding.get("agents", []):
                continue
            old_ref = next((s for s in current_skills if s.get("skill_id") == binding.get("id")), None)
            detaching = name in removed or ident not in expected.get(name, {}).get("agents", [])
            if binding.get("id") and old_ref is None and not detaching:
                raise ValueError("drift: managed Skill was removed externally: " + name)
            if old_ref and str(old_ref["version"]) != str(binding["version"]):
                desired = skill_versions.get(name)
                if not desired or str(old_ref["version"]) != str(desired["version"]):
                    raise ValueError("drift: managed Skill was changed externally: " + name)
        desired = writable_agent(current)
        skills = []
        managed_ids = {b.get("id"): name for name, b in
                       {**(previous_bindings or {}), **expected}.items() if b.get("id")}
        for ref in current_skills:
            name = managed_ids.get(ref.get("skill_id"))
            if name in removed:
                continue
            if name is not None and ident not in expected.get(name, {}).get("agents", []):
                continue
            if name in skill_versions:
                if ident not in expected[name].get("agents", []):
                    continue
                ref = {**ref, "version": skill_versions[name]["version"]}
            skills.append(ref)
        for name, version in skill_versions.items():
            if (ident in expected[name].get("agents", [])
                    and not any(s.get("skill_id") == version["id"] for s in skills)):
                skills.append({"type": "custom", "skill_id": version["id"], "version": version["version"]})
        if len(skills) > 50:
            raise ValueError("Agent exceeds 50 Skill references")
        desired["skills"] = skills
        if desired.get("multiagent"):
            for member in desired["multiagent"].get("agents") or []:
                if member.get("type") != "self" and member.get("id") in child_versions:
                    member["version"] = child_versions[member["id"]]["version"]
        if desired == writable_agent(current):
            return current

        async def reconcile(row):
            actual = await client.request("GET", f"agents/{ident}")
            fields = {k: v for k, v in desired.items() if k != "version"}
            if all(actual.get(k) == v for k, v in fields.items()):
                return actual
            return None

        try:
            return await effects.apply(
                f"{release_id}:agent:{ident}:{current['version']}", "agent-update",
                {"agent_id": ident, "payload": desired},
                lambda: client.request("POST", f"agents/{ident}", json=desired), reconcile,
            )
        except MAError as exc:
            if exc.status != 409 or attempt == 2:
                raise
    raise ValueError("Agent version conflict exhausted")


async def cleanup(services, client, connection, *, task=None):
    repo = MARepository(services.tx)
    async with services.tx.tenant_tx(connection["tenant_id"]) as conn:
        owned = await repo.effects(conn, connection["connection_id"])
        for effect in owned:
            if (effect["kind"].startswith("replay-") and "/" in effect["owner_id"]
                    and effect["status"] in {"confirmed", "outcome_unknown"}):
                owner_task = await services.tasks.get(conn, task_id=effect["owner_id"].split("/", 1)[0])
                if owner_task and owner_task["status"] in {"succeeded", "failed", "cancelled"}:
                    await repo.confirm_effect(conn, effect["effect_id"], effect["remote_id"],
                                              effect["receipt"], status="cleanup_pending")
        pending = await repo.effects(conn, connection["connection_id"], status="cleanup_pending")
    for effect in reversed(pending):
        remote = effect["remote_id"]
        if not remote:
            try:
                receipt = await recover_owned(client, effect)
            except Exception:
                receipt = None
            if not receipt:
                continue
            remote, effect["receipt"] = receipt["id"], receipt
            async with services.tx.tenant_tx(connection["tenant_id"]) as conn:
                if task:
                    await services.tasks.require_current(conn, task)
                await repo.confirm_effect(conn, effect["effect_id"], remote, receipt, status="cleanup_pending")
        # Ledger records are the only ownership authority. Never sweep by prefix.
        try:
            if not await delete_owned(client, effect["kind"], remote):
                continue
        except Exception:
            continue
        async with services.tx.tenant_tx(connection["tenant_id"]) as conn:
            if task:
                await services.tasks.require_current(conn, task)
            await repo.confirm_effect(conn, effect["effect_id"], remote, effect["receipt"], status="cleaned")
    async with services.tx.tenant_tx(connection["tenant_id"]) as conn:
        if task:
            await services.tasks.require_current(conn, task)
        await repo.retire_session_events(conn, connection["connection_id"])


async def reconcile_connection(task, services, client, connection, operation):
    repo, config = MARepository(services.tx), connection["config"]
    await cleanup(services, client, connection, task=task)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        previous = await repo.applied(conn, task.subject_id)
        resources = await repo.all_resources(conn, task.subject_id, kind="skill")
    if not previous:
        raise ValueError("Initial Skill discovery must complete before synchronization")
    checkpoint = operation["checkpoint"]
    # Don't send ETag for a partially applied release; it still needs recovery.
    binding_targets = {r["logical_name"]: sorted(r["record"].get("agents") or []) for r in resources}
    applied_targets = {n: sorted(b.get("agents") or []) for n, b in
                       (previous["record"].get("skills") or {}).items()}
    same_revision = previous["record"].get("connection_revision") == connection["revision"]
    etag = (previous["record"].get("pull_etag", "")
            if not checkpoint and same_revision and binding_targets == applied_targets else "")
    manifest, new_etag = await pull(services, task.tenant_id, config, etag)
    if manifest is None:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.checkpoint(conn, task.task_id, {}, {"status": "unchanged"})
            await repo.status(conn, task.subject_id, "ready", connection["detail"], synced=True)
        return
    published = decode_manifest(manifest)
    bindings = {r["logical_name"]: r["record"] for r in resources}
    selected = {n: published[n] for n, b in bindings.items() if b.get("agents") and n in published}
    removed = {n for n in bindings if n not in published}
    if removed:
        confirmed = await services.extras["ma_skill_tombstones"](task.tenant_id, list(removed))
        if set(confirmed) != removed:
            raise ValueError("Published manifest omission is not a confirmed Skill deletion")
    manifest_hash = digest([(n, s["version"], s["tree_sha256"]) for n, s in sorted(selected.items())])
    release_id = "ma-release:" + digest([connection["config_hash"], manifest_hash, bindings])[:40]
    effects = Effects(services, task.tenant_id, task.subject_id, release_id, task=task)
    skill_versions = {}
    previous_bindings = previous["record"].get("skills") or {}
    for name, skill in selected.items():
        binding = bindings[name]
        old = previous_bindings.get(name) or {}
        changed = (binding.get("tree_sha256") != skill["tree_sha256"]
                   or set(binding.get("agents") or []) != set(old.get("agents") or []))
        verifier = services.extras.get("ma_publication_target")
        if changed and verifier and not await verifier(task.tenant_id, name, skill["version"], task.subject_id):
            raise ValueError("MA target validation is missing for published Skill: " + name)
        if binding.get("tree_sha256") == skill["tree_sha256"]:
            skill_versions[name] = {**binding, "team_version": skill["version"]}
            continue
        remote = binding.get("id") or ""
        existing = await find_skill_version(client, remote, skill["tree_sha256"]) if remote else None
        if existing:
            receipt = existing
        else:
            archive = skill_archive(skill["package"])
            title = "te-managed-" + digest([task.tenant_id, task.subject_id, name, skill["tree_sha256"]])[:32]
            async def reconcile(row, sid=remote, tree=skill["tree_sha256"], owned_title=title):
                if sid:
                    return await find_skill_version(client, sid, tree)
                matches = [s for s in await client.listing("skills", source="custom")
                           if s.get("display_title") == owned_title]
                if len(matches) != 1:
                    return None
                return await find_skill_version(client, matches[0]["id"], tree)
            receipt = await effects.apply(
                release_id + ":skill:" + name, "skill-version",
                {"skill_id": remote, "name": name, "tree_sha256": skill["tree_sha256"],
                 "display_title": title, "zip_sha256": hashlib.sha256(archive).hexdigest()},
                lambda n=name, a=archive, sid=remote, t=title:
                    client.upload_skill(n, a, skill_id=sid, display_title=t if not sid else n), reconcile,
            )
        skill_versions[name] = {**binding, "id": remote or receipt["id"],
                                "version": str(receipt.get("version") or receipt.get("latest_version") or "1"),
                                "team_version": skill["version"], "tree_sha256": skill["tree_sha256"]}
    environment = previous["record"]["environment"]
    runtime_tree = previous["record"].get("runtime_tree_sha256", "")
    runtime_binding = config.get("runtime_binding") or {}
    if runtime_binding and runtime_binding["skill_name"] in selected:
        item = selected[runtime_binding["skill_name"]]
        base = previous["record"].get("base_environment") or environment
        runtime_config, runtime_tree = bind_runtime(base["config"], runtime_binding, item["package"].files)
        if digest(runtime_config) != digest(environment["config"]):
            desired = {"name": "te-release-" + digest([task.subject_id, runtime_config])[:32], "config": runtime_config}
            async def reconcile_environment(row):
                matches = [e for e in await client.listing("environments")
                           if e.get("name") == desired["name"]]
                if len(matches) != 1:
                    return None
                actual = await client.request("GET", f"environments/{matches[0]['id']}")
                return actual if actual.get("config") == runtime_config else None
            environment = await effects.apply(
                release_id + ":environment", "release-environment", desired,
                lambda: client.request("POST", "environments", json=desired), reconcile_environment,
            )
    # Fetch latest graph: preserve unrelated model/system/tool edits when merging.
    graph = await agent_graph(client, config["entry_agent_id"])
    if any(not set(b.get("agents") or []) <= set(graph["agents"]) for b in bindings.values()):
        raise ValueError("Target MA members are absent from the current Agent graph")
    versions = {}
    record = {"release_id": release_id, "manifest_hash": manifest_hash, "skills": skill_versions,
              "environment": environment, "environment_id": environment["id"],
              "environment_hash": digest(environment["config"]), "connection_revision": connection["revision"],
              "base_environment": previous["record"].get("base_environment") or previous["record"]["environment"],
              "runtime_tree_sha256": runtime_tree, "pull_etag": new_etag,
              "entry_agent_id": config["entry_agent_id"]}
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.save_release(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                release_id=release_id, manifest_hash=manifest_hash, record=record)
    # Graph dict is postorder: leaves before parents, root committed last.
    affected = set()
    for name in set(skill_versions) | removed:
        old, new = previous_bindings.get(name) or bindings.get(name) or {}, skill_versions.get(name) or {}
        if old.get("version") != new.get("version") or set(old.get("agents") or []) != set(new.get("agents") or []):
            affected.update(old.get("agents") or [])
            affected.update(new.get("agents") or [])
    for ident in graph_postorder(graph):
        members = (graph["agents"][ident].get("multiagent") or {}).get("agents") or []
        if any(child.get("id") in affected for child in members):
            affected.add(ident)
        value = (await update_agent(client, effects, ident, bindings, skill_versions, versions, removed, release_id,
                                    previous_bindings) if ident in affected else graph["agents"][ident])
        versions[ident] = {"id": ident, "version": value["version"], "name": value.get("name")}
        checkpoint["agents"] = versions
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.checkpoint(conn, task.task_id, checkpoint)
    readback = await agent_graph(client, config["entry_agent_id"], versions[config["entry_agent_id"]]["version"])
    for name, version in skill_versions.items():
        for ident in version["agents"]:
            attached = readback["agents"][ident].get("skills") or []
            if not any(s.get("skill_id") == version["id"] and str(s["version"]) == version["version"]
                       for s in attached):
                raise ValueError("MA Skill reference readback mismatch")
    actual_environment = await client.request("GET", f"environments/{environment['id']}")
    if digest(actual_environment["config"]) != record["environment_hash"]:
        raise ValueError("MA runtime Environment readback mismatch")
    record.update({"agents": versions, "entry_agent_version": str(versions[config["entry_agent_id"]]["version"]),
                   "agent_graph": readback, "readback_verified": True})
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.save_release(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                release_id=release_id, manifest_hash=manifest_hash, record=record, status="applied")
        for name, version in skill_versions.items():
            await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                            kind="skill", name=name, record=version)
        for ident, version in versions.items():
            await repo.bind(conn, tenant_id=task.tenant_id, connection_id=task.subject_id, kind="agent", name=ident,
                            record={**version, "snapshot": readback["agents"][ident]})
        await repo.checkpoint(conn, task.task_id, checkpoint, {"status": "applied", "release_id": release_id})
        await repo.status(conn, task.subject_id, "ready", connection["detail"], synced=True)
