"""Durable Miner Compile tracking and post-success indexing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from team_miner.shared.document_converter import convert_knowledge_upload
from teamEvolver.runtime import HandlerResult

from .links import build_index
from .openviking import (
    clients,
    documents,
    ensure_mining_skill,
    ensure_workspace,
    request,
)
from .state import MinerState, digest


async def guard(conn, services, task):
    await services.tasks.require_current(conn, task, allow_cancel=True)
    return (await services.tasks.get(conn, task_id=task.task_id))["cancel_requested"]


async def track(task, services):
    state = MinerState(services.tx, task.tenant_id)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        payload = await state.get(conn, "payload", task.task_id)
        receipt = await state.get(conn, "receipt", task.task_id)
    if payload is None or digest(payload) != task.payload_hash:
        return HandlerResult.fail("miner_payload_hash_mismatch")
    if receipt and receipt.get("finalized"):
        if receipt["status"] == "cancelled":
            return HandlerResult.cancelled()
        return HandlerResult.succeed(task.task_id) if receipt["status"] == "succeeded" else HandlerResult.fail(
            "miner_compile_" + receipt["status"])
    settings = payload["settings"]
    client, compiler = await clients(services, settings)
    try:
        await ensure_workspace(client, settings)
        if payload["operation"] == "compile":
            await ensure_mining_skill(
                client,
                compiler,
                settings["skill_uri"],
                task.task_id + ":bootstrap-skill",
            )
        if payload["operation"] == "upload":
            return await upload(task, services, state, client, payload)
        if payload["operation"] == "delete":
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                if await guard(conn, services, task):
                    return HandlerResult.cancelled()
            await request(client, "DELETE", "/api/v1/fs",
                          params={"uri": payload["uri"], "recursive": "true"}, key=task.task_id)
            return HandlerResult.succeed(task.task_id)
        if payload["operation"] == "graph":
            docs = await documents(client, settings["target_uri"])
            from .wiki_graph import build_wiki_graph, render_wiki_graph_html
            index = build_index(docs, settings["target_uri"])
            result = {"graph": build_wiki_graph(docs, index, wiki_root=settings["target_uri"])}
            result["html"] = render_wiki_graph_html(result["graph"], title="Knowledge Wiki")
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await guard(conn, services, task)
                await state.put(conn, "result", task.task_id, result)
            return HandlerResult.succeed(task.task_id)
        if not receipt or not receipt.get("upstream_task_id"):
            recovering = receipt is not None
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                cancelled = await guard(conn, services, task)
                if cancelled and not receipt:
                    return HandlerResult.cancelled()
                if not receipt:
                    receipt = {"job_id": task.task_id, "upstream_task_id": "",
                               "settings": settings, "started_at": payload["started_at"], "status": "running"}
                    await state.put(conn, "receipt", task.task_id, receipt)
            compile_payload = payload["compile"]
            accepted = None
            if recovering:
                from teamEvolver.openviking import OpenVikingError
                try:
                    accepted = await compiler.submission(task.task_id)
                except OpenVikingError as exc:
                    if exc.status_code != 404:
                        raise
            if accepted is None:
                accepted = await compiler.submit(kind="miner.compile", payload=compile_payload,
                                                  idempotency_key=task.task_id)
            receipt = {**receipt, "upstream_task_id": accepted.task_id}
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await guard(conn, services, task)
                await state.put(conn, "receipt", task.task_id, receipt)
                await state.put(conn, "upstream", [settings["account_id"], accepted.task_id], receipt)
                await conn.execute(
                    f"UPDATE {services.tx.pool.schema}.worker_tasks SET upstream_task_id=$2 WHERE task_id=$1",
                    task.task_id, accepted.task_id)
        while True:
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                cancelled = await guard(conn, services, task)
            if cancelled:
                await compiler.cancel(receipt["upstream_task_id"])
            status = await compiler.status(receipt["upstream_task_id"])
            if status.terminal:
                break
            await asyncio.sleep(0.5)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            cancelled = await guard(conn, services, task)
            terminal = "cancelled" if cancelled else status.state.value
            fresh = await state.finalize(conn, receipt, terminal)
            if fresh and terminal == "succeeded":
                rid = task.task_id + ":index"
                await state.put(conn, "payload", rid, {"settings": settings, "compile_task_id": status.task_id})
                await services.tasks.enqueue(conn, tenant_id=task.tenant_id, task_id=rid, kind="miner.rebuild_index",
                                             subject_type="miner_index", subject_id=settings["target_uri"],
                                             payload_ref=rid, payload_hash=digest(settings), dedupe_key=rid,
                                             config_revision=task.config_revision)
        if terminal == "cancelled":
            return HandlerResult.cancelled()
        if terminal == "failed":
            return HandlerResult.fail("miner_compile_failed")
        return HandlerResult.succeed(task.task_id)
    except Exception:
        if receipt and payload["operation"] == "compile":
            return HandlerResult.reconcile()
        raise
    finally:
        await client.aclose()


async def upload(task, services, state, client, payload):
    import base64

    from .schemas import MAX_TOTAL_BYTES
    total, files = 0, []
    for item in payload["files"]:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if await guard(conn, services, task):
                return HandlerResult.cancelled()
            checkpoint = await state.get(conn, "upload", [task.task_id, item["path"]])
        if checkpoint:
            files.append(checkpoint)
            total += checkpoint["size"]
            continue
        converted = await asyncio.to_thread(
            convert_knowledge_upload, item["path"], base64.b64decode(item["content_b64"]))
        content = converted.markdown
        total += len(content.encode())
        if total > MAX_TOTAL_BYTES:
            return HandlerResult.fail("miner_normalized_upload_limit")
        path = item["path"].rsplit(".", 1)[0] + ".md"
        uri = payload["directory"] + "/" + path
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if await guard(conn, services, task):
                return HandlerResult.cancelled()
        await request(client, "POST", "/api/v1/content/write",
                      body={"uri": uri, "content": content, "mode": "replace", "wait": False},
                      key=digest([task.task_id, path]))
        checkpoint = {"uri": uri, "size": len(content.encode()), "source_format": converted.source_format}
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard(conn, services, task)
            await state.put(conn, "upload", [task.task_id, item["path"]], checkpoint)
        files.append(checkpoint)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await guard(conn, services, task)
        await state.put(conn, "result", task.task_id, {"files": files, "total_bytes": total})
    return HandlerResult.succeed(task.task_id)


async def rebuild(task, services):
    state = MinerState(services.tx, task.tenant_id)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        payload = await state.get(conn, "payload", task.task_id)
    settings = payload["settings"]
    if task.payload_hash != digest(settings):
        return HandlerResult.fail("miner_index_payload_hash_mismatch")
    client, _ = await clients(services, settings)
    try:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if await guard(conn, services, task):
                return HandlerResult.cancelled()
            await state.lock(conn, state.scope(settings))
            watermark = await state.get(conn, "watermark", state.scope(settings))
            if not watermark or watermark["task_id"] != payload["compile_task_id"]:
                return HandlerResult.succeed(task.task_id)
            existing = await state.get(conn, "index", state.scope(settings))
            if existing and existing.get("compile_task_id") == payload["compile_task_id"]:
                return HandlerResult.succeed(task.task_id)
        docs = await documents(client, settings["target_uri"])
        index = build_index(docs, settings["target_uri"], payload["compile_task_id"])
        index["generated_at"] = datetime.now(timezone.utc).isoformat()
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if await guard(conn, services, task):
                return HandlerResult.cancelled()
            await state.lock(conn, state.scope(settings))
            watermark = await state.get(conn, "watermark", state.scope(settings))
            if not watermark or watermark["task_id"] != payload["compile_task_id"]:
                return HandlerResult.succeed(task.task_id)
            await state.put(conn, "index", state.scope(settings), index)
        return HandlerResult.succeed(task.task_id)
    finally:
        await client.aclose()


track.reconcile_cancellation = True


def register_handlers():
    return {"miner.track_compile": track, "miner.rebuild_index": rebuild}
