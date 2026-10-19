"""Durable aggregation, deterministic staging, tree reduction and maintenance."""
from __future__ import annotations

from teamEvolver.context.repository import stable_hash
from teamEvolver.openviking import ensure_directories
from teamEvolver.security.agent_principal import normalize_user_id

from .aggregation.sources import DEFAULT_MEMORY_KINDS
from .merge import compile_stage, tree_levels
from .openviking import clients, copy_tree, ensure_skill, get_skill, request, snapshot_tree
from .repository import MemoryRepository
from .runs import guard


async def pipeline(task, services, payload, renew):
    repo = MemoryRepository(services.tx)
    agg = payload["config"].get("aggregation", payload["config"])
    work = f"viking://user/{payload['user_id']}/resources/teamEvolver/staging/{task.task_id.replace(':', '-')}"
    client, compiler = await clients(services, payload)
    async def durable(key, fn):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task)
            stage = await repo.stage(conn, task.task_id, key)
        if stage and stage["status"] == "succeeded":
            return stage["result"]
        await renew()
        result = await fn()
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task)
            await repo.save_stage(conn, task, key, {}, status="succeeded", result=result)
        return result
    try:
        if payload.get("operation") == "publish_skill":
            uri = payload["skill_uri"]
            name = uri.rsplit("/", 1)[-1]
            current = await get_skill(compiler, name)
            await renew()
            result = await request(client, "PUT" if current.ok else "POST",
                                   "/api/v1/skills/" + name if current.ok else "/api/v1/skills",
                                   body={"data": payload["skill_body"], "wait": False,
                                         "target_uri": "viking://agent/skills"},
                                   key=task.task_id, shared_admin=True)
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await guard(conn, services, task)
                await repo.finish(conn, task, payload, {"skill_uri": uri, "result": result})
            return result
        await ensure_directories(client, [payload["target_uri"], work])

        async def inventory_users():
            users = payload["user_ids"]
            if users is not None:
                return {"users": users}
            size = max(1, min(1000, int(agg.get("account_user_page_size", 1000))))
            maximum = max(1, min(1_000_000, int(agg.get("account_user_limit", 50_000))))
            users = []
            while True:
                await renew()
                page = await request(client, "GET", f"/api/v1/admin/accounts/{payload['account_id']}/users",
                                     params={"limit": min(size, maximum + 1 - len(users)), "offset": len(users)})
                rows = page.get("users", page.get("items", [])) if isinstance(page, dict) else page
                if not isinstance(rows, list):
                    raise ValueError("invalid account user inventory")
                values = [normalize_user_id(r if isinstance(r, str) else r.get("user_id", r.get("id"))) for r in rows]
                if set(values) & set(users):
                    raise ValueError("user pagination did not advance")
                users.extend(values)
                if len(users) > maximum:
                    raise ValueError("account user safety limit exceeded")
                if len(values) < size:
                    break
            return {"users": [u for u in users if u != payload["user_id"]]}

        async def pin(stage, uri):
            await renew()
            current = await ensure_skill(client, compiler, stage, uri, task.task_id + ":bootstrap:" + stage)
            destination = f"{work}/skills/{stage}"
            await ensure_directories(client, [f"{work}/skills"])
            await copy_tree(client, uri, destination, task.task_id + ":skill:" + stage, shared_admin=True)
            return {"uri": destination, "revision": str((current.data or {}).get("revision") or
                                                       stable_hash(current.data))}

        before = await durable("change:before", lambda: snapshot_tree(
            client, payload["target_uri"], work + "/before", task.task_id + ":before"))
        sources, changed = [], []
        if payload["pipeline"] in {"aggregation", "both"}:
            pinned = await durable("skill:aggregation", lambda: pin(
                "aggregation", agg.get("okf_skill_uri", "viking://agent/skills/team-memory-okf")))
            users = (await durable("users", inventory_users))["users"]
            kinds = payload["kinds"] or list(DEFAULT_MEMORY_KINDS)
            for user in users:
                async def stage_user(user=user):
                    reader, _ = await clients(services, payload, user=user)
                    root = f"viking://user/{user}/memories"
                    try:
                        from teamEvolver.openviking import OpenVikingError
                        try:
                            tree = await request(reader, "GET", "/api/v1/fs/tree",
                                                 params={"uri": root, "node_limit": 10_000, "level_limit": 20,
                                                         "output": "original"})
                        except OpenVikingError as exc:
                            if exc.status_code != 404:
                                raise
                            tree = []
                        entries = tree.get("entries", tree.get("items", [])) if isinstance(tree, dict) else tree
                        if not isinstance(entries, list) or len(entries) >= 10_000:
                            raise ValueError("Memory inventory incomplete or exceeds node limit")
                        files = [e for e in entries if not e.get("isDir", e.get("is_dir"))
                                 and str(e.get("uri", "")).startswith(root + "/")
                                 and str(e["uri"])[len(root)+1:].split("/")[0].removesuffix(".md") in kinds]
                        files.sort(key=lambda e: e["uri"])
                        destination = f"{work}/users/{stable_hash(user)[:16]}"
                        async with services.tx.tenant_tx(task.tenant_id) as conn:
                            prior = await repo.checkpoint(conn, payload["account_id"], payload["target_uri"], user)
                        documents, total = [], 0
                        for entry in files:
                            await renew()
                            text = await request(reader, "GET", "/api/v1/content/read", params={"uri": entry["uri"]})
                            if not isinstance(text, str):
                                raise ValueError("Memory source must be UTF-8 text")
                            total += len(text.encode())
                            if total > 20 * 1024 * 1024:
                                raise ValueError("Memory user staging exceeds 20 MiB")
                            documents.append({"source_uri": entry["uri"], "content": text})
                        fingerprint = stable_hash(documents)
                        if (not payload["full"] and prior and prior["source_hash"] == fingerprint
                                and prior["skill_revision"] == pinned["revision"]):
                            return {"uri": prior["result_ref"], "fingerprint": fingerprint, "reused": True}
                        if not documents:
                            return {"uri": "", "fingerprint": fingerprint, "empty": True}
                        import json
                        await renew()
                        await ensure_directories(client, [destination])
                        await request(client, "POST", "/api/v1/content/write",
                                      body={"uri": destination + "/snapshot.jsonl",
                                            "content": "\n".join(json.dumps(d, ensure_ascii=False) for d in documents),
                                            "mode": "replace", "wait": False},
                                      key=stable_hash([task.task_id, user]))
                        return {"uri": destination, "fingerprint": fingerprint}
                    finally:
                        await reader.aclose()
                staged = await durable("user:" + user, stage_user)
                if staged["uri"]:
                    sources.append(staged["uri"])
                    if not staged.get("reused"):
                        changed.append(staged["uri"])
            if agg.get("preserve_manual_edits"):
                baseline = work + "/baseline"
                await durable("baseline", lambda: copy_tree(client, payload["target_uri"], baseline,
                                                            task.task_id + ":baseline"))
                sources.insert(0, baseline)
            if sources:
                fan_in = max(2, min(15, int(agg.get("merge_fan_in", 4))))
                groups_to_compile = []
                if agg.get("incremental_publish"):
                    selected = sources if payload["full"] else changed
                    size = max(1, min(fan_in, int(agg.get("publish_batch_users", 8))))
                    groups_to_compile = [[(f"batch:{i}", selected[i:i+size], payload["target_uri"])]
                                         for i in range(0, len(selected), size)]
                elif len(users) >= int(agg.get("partition_threshold", 512)):
                    count = max(16, min(1024, int(agg.get("partition_count", 256))))
                    partitions = {}
                    for source in sources:
                        bucket = int(stable_hash(source.rsplit("/", 1)[-1])[:8], 16) % count
                        partitions.setdefault(bucket, []).append(source)
                    for bucket, inputs in sorted(partitions.items()):
                        for level in tree_levels(inputs, fan_in, work + f"/partition-{bucket}"):
                            groups_to_compile.append([
                                (f"partition:{bucket}:{key}", group,
                                 output or payload["target_uri"] + f"/partition-{bucket:04d}")
                                for key, group, output in level])
                else:
                    groups_to_compile = tree_levels(sources, fan_in, work)
                for groups in groups_to_compile:
                    for key, inputs, output in groups:
                        body = {"from": inputs, "to": output or payload["target_uri"], "skill": pinned["uri"],
                                "instruction": "Aggregate team facts; preserve provenance and human corrections."}
                        if payload["last_compile_time"]:
                            body["args"] = {"last_compile_time": payload["last_compile_time"]}
                        await compile_stage(task, services, client, compiler, key, body, renew)
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    await guard(conn, services, task)
                    for user in users:
                        staged = await repo.stage(conn, task.task_id, "user:" + user)
                        await repo.save_checkpoint(conn, task, payload["account_id"], payload["target_uri"], user,
                                                   staged["result"]["fingerprint"], pinned["revision"],
                                                   staged["result"]["uri"])
        snapshot = ""
        if payload["pipeline"] in {"maintenance", "both"}:
            pinned = await durable("skill:maintenance", lambda: pin(
                "maintenance", agg.get("maintenance_skill_uri", "viking://agent/skills/team-memory-maintenance")))
            snapshot = work + "/maintenance"
            await durable("maintenance:snapshot", lambda: copy_tree(
                client, payload["target_uri"], snapshot, task.task_id + ":maintenance:snapshot"))
            await compile_stage(task, services, client, compiler, "maintenance",
                                {"from": [snapshot], "to": payload["target_uri"], "skill": pinned["uri"]}, renew)
        result = {"target_uri": payload["target_uri"], "source_count": len(sources), "snapshot_uri": snapshot}
        after = await durable("change:after", lambda: snapshot_tree(
            client, payload["target_uri"], work + "/after", task.task_id + ":after"))
        result["after_ref"] = after["uri"]
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task)
            await repo.finish(conn, task, payload, result, before["uri"])
        return result
    finally:
        await client.aclose()
