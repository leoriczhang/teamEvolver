"""Resumable complete MA Session ranges into mandatory ingestion."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

from teamEvolver.managed_agents.events import child_threads, complete, metrics, stable_end, tag_values, turns
from teamEvolver.storage.postgres import PostgresObjectStore

from .discovery import agent_graph
from .repository import MARepository
from .schemas import canonical, digest

PROJECTION_REVISION = "ma-child-traces-v3"


async def save_object(services, tenant_id, category, data):
    sha = hashlib.sha256(data).hexdigest()
    key = f"ma/{category}/{sha}"
    async with services.tx.tenant_tx(tenant_id) as conn:
        await PostgresObjectStore(services.tx, tenant_id).put(conn, key, data)
    return {"object_ref": key, "sha256": sha, "size": len(data)}


async def session_events(client, session_id):
    main = complete(await client.history(session_id))
    threads = await client.listing(f"sessions/{session_id}/threads", max_pages=10)
    child_events, children_done = [], True
    for thread in child_threads(main, threads):
        # Main timeline may also contain child messages: complete() deduplicates.
        history = complete(await client.history(session_id, thread["id"]))
        child_events.extend({**event, "session_thread_id": thread["id"]} for event in history)
        if thread.get("status") not in {"idle", "terminated", "failed", "completed"}:
            children_done = False
        elif thread.get("status") == "idle":
            end = history[-1] if history else {}
            if end.get("stop_reason", {}).get("type") == "requires_action":
                children_done = False
    # Merge by processed time, including stable ordering for equal timestamps.
    combined = complete(main + child_events)
    combined.sort(key=lambda event: str(event.get("processed_at") or "\uffff"))
    return main, combined, children_done


async def runtime_snapshot(client, session):
    ref = session.get("agent") or {}
    if not isinstance(ref, dict) or not ref.get("id") or ref.get("version") is None:
        return {"snapshot_missing": True, "snapshot_reason": "Session Agent version is unavailable"}
    overrides = {k: v for k, v in ref.items() if k not in
                 {"id", "type", "version", "overridden_fields", "base_agent", "scene", "created_at", "updated_at"}}
    if ref.get("overridden_fields") and not overrides:
        return {"snapshot_missing": True, "snapshot_reason": "Session overrides are unavailable"}
    graph = await agent_graph(client, ref["id"], ref["version"], overrides=overrides)
    environment = session.get("environment")
    # Environments are unversioned. Reading today's Environment cannot establish
    # what executed historically; fail closed if Session has no config snapshot.
    if not isinstance(environment, dict) or not isinstance(environment.get("config"), dict):
        return {"agent_graph": graph, "snapshot_missing": True,
                "snapshot_reason": "Session Environment config snapshot is unavailable"}
    return {"agent_graph": graph, "environment": environment, "snapshot_missing": False}


async def materials(client, events, services, tenant_id, resources=()):
    output, missing, total, seen = [], [], 0, set()
    missing.extend({"resource_type": resource.get("type"), "reason": "input resource bytes are unavailable"}
                   for resource in resources if resource.get("type") != "file")
    inputs = [event for event in events if event.get("type") == "user.message"]
    inputs.extend({"type": "user.message", "content": [{"type": "document", "source": {
        "type": "file", "file_id": resource.get("file_id"), "mount_path": resource.get("mount_path"),
    }}]} for resource in resources if resource.get("type") == "file")
    for event in inputs:
        if event.get("type") != "user.message":
            continue
        for block in event.get("content") or []:
            if block.get("type") == "text":
                continue
            source = block.get("source") or {}
            ident = str(source.get("file_id") or "")
            try:
                if source.get("type") == "base64":
                    data = base64.b64decode(source["data"], validate=True)
                elif source.get("type") == "text":
                    data = str(source.get("data") or "").encode()
                elif ident:
                    record = await client.request("GET", f"files/{ident}")
                    url = record.get("download_url")
                    if not url:
                        raise ValueError("File download URL is unavailable")
                    data = await download_url(client, url, 20 * 1024 * 1024)
                else:
                    # URL attachments need explicit archived bytes; do not invent inputs.
                    raise ValueError("External attachment bytes are unavailable")
                sha = hashlib.sha256(data).hexdigest()
                if sha in seen:
                    continue
                if len(data) > 20 * 1024 * 1024 or total + len(data) > 80 * 1024 * 1024 or len(output) >= 100:
                    raise ValueError("Input material limit exceeded")
                seen.add(sha)
                total += len(data)
                saved = await save_object(services, tenant_id, "materials", data)
                output.append({"path": source.get("mount_path") or f"uploads/{ident or sha[:20]}", **saved,
                               "content_b64": base64.b64encode(data).decode(),
                               "media_type": source.get("media_type", "application/octet-stream"),
                               "original_file_id": ident})
            except (ValueError, KeyError, RuntimeError):
                missing.append({"file_id": ident, "reason": "input bytes unavailable or limit exceeded"})
    return output, missing


async def download_url(client, url, limit):
    from teamEvolver.managed_agents.client import MAError, _bounded, validate_url
    validate_url(url.split("?", 1)[0], allow_loopback=client.allow_loopback)
    async with client.download_http.stream("GET", url) as response:
        if response.status_code != 200:
            raise MAError(response.status_code, "FileDownloadFailed")
        return await _bounded(response, limit)


async def collect_one(task, services, client, connection, session_id, excluded):
    repo, config = MARepository(services.tx), connection["config"]
    if session_id in excluded:
        return "replay_excluded"
    session = await client.request("GET", f"sessions/{session_id}")
    tags = tag_values(session.get("tags"))
    if any(str(tag).startswith("te-replay:") for tag in tags):
        return "replay_excluded"
    main, all_events, children_done = await session_events(client, session_id)
    end = stable_end(main)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        cursor = await repo.cursor(conn, task.subject_id, session_id)
    previous = (cursor or {}).get("record") or {}
    record = {**previous, "status": session.get("status"), "last_pulled_at": datetime.now(timezone.utc).isoformat()}
    formal_session_id = "session_ma_" + digest([task.subject_id, session_id])[:32]
    events_hash = digest(all_events)
    if not end or not children_done or session.get("status") in {"running", "rescheduling", "requires_action"}:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.save_cursor(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                   session_id=session_id, record=record)
        return "active"
    migrating_legacy_ranges = (
        previous.get("end_event_id") == end["id"]
        and previous.get("formal_session_id") != formal_session_id
    )
    refresh_projection = bool(previous) and previous.get("projection_revision") != PROJECTION_REVISION
    same_boundary = previous.get("end_event_id") == end["id"]
    if (same_boundary and not migrating_legacy_ranges and not refresh_projection
            and previous.get("events_hash") == events_hash):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.save_cursor(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                   session_id=session_id, record=record)
        return "unchanged"
    last = None if migrating_legacy_ranges or refresh_projection or same_boundary else previous.get("end_event_id")
    if last and last not in {e["id"] for e in main}:
        raise ValueError("Previously accepted MA range boundary has expired")
    index = next((i + 1 for i, e in enumerate(main) if e["id"] == last), 0)
    end_index = next(i + 1 for i, e in enumerate(main) if e["id"] == end["id"])
    new_main = main[index:end_index]
    accepted_main = main[:end_index]
    range_start_time = str(new_main[0].get("processed_at") or "") if new_main else ""
    session_start_time = str(accepted_main[0].get("processed_at") or "") if accepted_main else ""
    range_ids = {e["id"] for e in new_main}
    new_events = [e for e in all_events if e["id"] in range_ids or
                  (e.get("session_thread_id")
                   and range_start_time <= str(e.get("processed_at") or ""))]
    accepted_ids = {e["id"] for e in accepted_main}
    accepted_events = [e for e in all_events if e["id"] in accepted_ids or
                       (e.get("session_thread_id")
                        and session_start_time <= str(e.get("processed_at") or ""))]
    resource_rows = []
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        resource_rows = await repo.all_resources(conn, task.subject_id, kind="skill")
    skill_names = {r["record"]["id"]: r["logical_name"] for r in resource_rows}
    runtime = await runtime_snapshot(client, session)
    binding = config.get("runtime_binding") or {}
    working = str(binding.get("working_directory") or "").rstrip("/")
    skill_paths = {
        working + "/" + str(path.get("workspace_prefix") or "").strip("/") + "/": binding["skill_name"]
        for path in binding.get("paths") or []
        if working and binding.get("skill_name") and path.get("workspace_prefix")
    }
    new_turns = turns(new_events, skill_names, skill_paths)
    if not new_turns or not any(t.get("response_text") or len(t["messages"]) > 1 for t in new_turns):
        return "empty"
    projected = turns(accepted_events, skill_names, skill_paths)
    attribution = [str(t)[len(config["user_tag_prefix"]):] for t in tags
                   if str(t).startswith(config["user_tag_prefix"])]
    if len(set(attribution)) > 1:
        raise ValueError("Conflicting MA user attribution tags")
    user_id = attribution[0] if attribution else config["default_user_id"]
    from teamEvolver.security.agent_principal import normalize_user_id
    user_id = normalize_user_id(user_id)
    raw = await save_object(services, task.tenant_id, "events", canonical(accepted_events).encode())
    source_materials, missing = await materials(
        client, accepted_main, services, task.tenant_id, session.get("resources") or [],
    )
    available = []
    for agent in (runtime.get("agent_graph") or {}).get("agents", {}).values():
        for ref in agent.get("skills") or []:
            name = next((r["logical_name"] for r in resource_rows if r["record"].get("id") == ref.get("skill_id")), "")
            if name:
                available.append({"name": name, "source": "configured", "used": False})
    for turn in projected:
        turn["skill_refs"] = available + turn["skill_refs"]
    runtime.update({"type": "managed-agents", "connection_id": task.subject_id,
                    "connection_revision": connection["revision"], "materials_missing": missing})
    range_id = "ma-range:" + digest([task.subject_id, session_id, end["id"]])[:40]
    envelope = {
        "schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0",
        "session_id": formal_session_id, "source": "managed-agents", "runtime": runtime,
        "title": str(session.get("title") or "").strip()[:120],
        "runtime_context": {"user_id": user_id, "account_id": task.tenant_id,
                            "source_session_id": session_id, "connection_id": task.subject_id,
                            "formal_session_id": formal_session_id,
                            "user_attribution": "tag" if attribution else "connection_default"},
        "turns": projected, "messages": [m for t in projected for m in t["messages"]],
        "business_time": projected[0]["timestamp"], "timestamp": projected[0]["timestamp"],
        "source_materials": source_materials, "metrics": metrics(accepted_events),
        "meta": {"ma_connection_id": task.subject_id, "ma_parent_session_id": session_id,
                 "ma_range_id": range_id, "ma_end_event_id": end["id"],
                 "business_time": projected[0]["timestamp"], "pulled_at": record["last_pulled_at"],
                 "user_id": user_id, "user_attribution": "tag" if attribution else "connection_default",
                 "raw_events": raw},
    }
    job = await services.extras["ma_accept_session"](
        tenant_id=task.tenant_id, source="pull", external_id=range_id, user_id=user_id, account_id=task.tenant_id,
        session=envelope, business_time=projected[0]["timestamp"],
        dedupe_key=f"{range_id}:{PROJECTION_REVISION}:{events_hash}" + (":migrate" if refresh_projection else ""),
    )
    record.update({"end_event_id": end["id"], "range_id": range_id, "formal_session_id": formal_session_id,
                   "projection_revision": PROJECTION_REVISION, "events_hash": events_hash,
                   "job_id": job.job_id,
                   "business_time": projected[0]["timestamp"], "user_id": user_id})
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.save_cursor(conn, tenant_id=task.tenant_id, connection_id=task.subject_id,
                               session_id=session_id, record=record)
    return "duplicate" if job.dedupe else "accepted"


async def collect_connection(task, services, client, connection, operation):
    repo, checkpoint, request = MARepository(services.tx), operation["checkpoint"], operation["input"]["request"]
    explicit = request.get("session_ids")
    if explicit is not None and (not isinstance(explicit, list) or len(explicit) > 100):
        raise ValueError("session_ids must contain at most 100 Sessions")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        excluded = await repo.replay_session_ids(conn, task.subject_id)
        known = await repo.cursors(conn, task.subject_id, after=checkpoint.get("known_after", ""), limit=51)
    ids = list(explicit or [])
    next_page = None
    if explicit is None:
        checkpoint.setdefault("from_timestamp", request.get("from_timestamp") or
                              (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        if not checkpoint.get("remote_complete"):
            page = await client.page("sessions", agent_id=connection["config"]["entry_agent_id"],
                                     **({"page": checkpoint["page"]} if checkpoint.get("page") else {}), limit=50)
            since, until = checkpoint["from_timestamp"], request.get("to_timestamp") or "\uffff"
            ids.extend(s["id"] for s in page.get("data", []) if since <= str(s.get("created_at") or "") <= until)
            next_page = page.get("next_page")
        if not checkpoint.get("known_complete"):
            ids.extend(c["session_id"] for c in known[:50])
    counts = dict(checkpoint.get("counts") or {})
    for ident in list(dict.fromkeys(ids))[:100]:
        if ident in checkpoint.get("completed", []):
            continue
        status = await collect_one(task, services, client, connection, ident, excluded)
        counts[status] = counts.get(status, 0) + 1
        checkpoint.setdefault("completed", []).append(ident)
        checkpoint["counts"] = counts
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.checkpoint(conn, task.task_id, checkpoint)
    more_known = not checkpoint.get("known_complete") and len(known) > 50 and explicit is None
    result = {"counts": counts, "next_page": next_page, "more_known": more_known}
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.checkpoint(conn, task.task_id, checkpoint, result)
        await repo.status(conn, task.subject_id, "ready", connection["detail"], collected=True)
        if next_page or more_known:
            from .commands import enqueue
            progress = {"page": next_page, "remote_complete": not next_page,
                        "known_after": known[49]["session_id"] if more_known else "",
                        "known_complete": not more_known, "from_timestamp": checkpoint["from_timestamp"]}
            follow = await enqueue(conn, services, tenant_id=task.tenant_id, connection_id=task.subject_id,
                                   kind="ma.collect", body={**request, "page": next_page},
                                   operation_id="ma-collect:" + digest([task.task_id, progress])[:40])
            # Follow-up uses accepted pagination input and a bounded fresh task.
            await repo.checkpoint(conn, follow["job_id"], progress)
    return result
