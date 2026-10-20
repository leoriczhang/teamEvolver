"""Durable source operations executed by the Skill-mining Worker."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from pathlib import PurePosixPath

from starlette.exceptions import HTTPException

from team_miner.shared.document_converter import convert_knowledge_upload

from .content_io import _content_text, content_editable
from .ov_compile_mining import SKILL_URI, normalize_source_tags, utc_now, validate_descendant, validate_resource_uri
from .platform_client import settings_parts
from .task_io import State


def source_inventory(record):
    uri = str(record.get("uri") or "")
    return {"source_id": record.get("source_id"), "account": record.get("account"), "path": uri, "uri": uri,
            "name": record.get("name") or uri.rsplit("/", 1)[-1],
            "tags": normalize_source_tags(record.get("tags"), default=("skill",)),
            "default": bool(record.get("default")), "created_at": record.get("created_at") or "",
            "document_count": 0, "total_bytes": 0, "ready": False, "status": "unverified"}


def safe_uri(value, *, allow_root=False):
    uri = validate_resource_uri(value, allow_root=allow_root)
    if "\\" in uri or "%" in uri or any(part.startswith(".") for part in uri.split("/")[3:]):
        raise ValueError("知识源路径不能包含隐藏目录或编码路径")
    return uri


def child(record, value, *, allow_root=False):
    return validate_descendant(safe_uri(record["uri"]), safe_uri(value), allow_root=allow_root)


async def guard(engine):
    task = getattr(engine, "task", None)
    if task is not None:
        async with engine.services.tx.tenant_tx(engine.tenant) as conn:
            await engine.services.tasks.require_current(conn, task)


async def request(engine, account, method, path, **kwargs):
    await guard(engine)
    task = getattr(engine, "task", None)
    if method not in {"GET", "HEAD"} and task is not None:
        stamp = hashlib.sha256(str([path, kwargs.get("json"), kwargs.get("params")]).encode()).hexdigest()[:32]
        kwargs["extra_headers"] = {"Idempotency-Key": f"{task.task_id}:{stamp}"}
    return await engine._knowledge_request(account, method, path, **kwargs)


async def checkpoint(engine, value=None):
    task = getattr(engine, "task", None)
    if task is None:
        return None
    state = State(engine.services, engine.tenant)
    async with engine.services.tx.tenant_tx(engine.tenant) as conn:
        await engine.services.tasks.require_current(conn, task)
        if value is not None:
            await state.put(conn, "source_upload", task.task_id, value)
        return await state.get(conn, "source_upload", task.task_id)


async def prepared_uploads(body):
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise ValueError("单次需上传 1 到 50 个文件")
    result, seen, total, normalized_total = [], set(), 0, 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("上传文件格式无效")
        relative = str(item.get("relative_path") or item.get("name") or "").replace("\\", "/")
        if (not relative or ":" in relative or any(part in {"", ".", ".."} or part.startswith(".")
                                                   for part in relative.split("/"))):
            raise ValueError("上传路径无效")
        try:
            raw = base64.b64decode(item["content_b64"], validate=True)
        except (KeyError, binascii.Error, ValueError, TypeError) as exc:
            raise ValueError("文件编码无效") from exc
        total += len(raw)
        if len(raw) > 20 * 1024 * 1024 or total > 100 * 1024 * 1024:
            raise HTTPException(413, "上传文件超过大小限制")
        converted = await asyncio.to_thread(convert_knowledge_upload, PurePosixPath(relative).name, raw)
        normalized_total += len(converted.markdown.encode())
        if normalized_total > 100 * 1024 * 1024:
            raise HTTPException(413, "转换后的知识源超过 100 MB")
        path = str(PurePosixPath(relative).with_suffix(".md"))
        if path in seen:
            raise ValueError("多个文件转换为同一路径")
        seen.add(path)
        result.append((path, converted.markdown))
    return result


async def upload(engine, record, body):
    account, root = record["account"], record["uri"]
    parent = child(record, body.get("parent_uri") or root, allow_root=True)
    plan = await checkpoint(engine)
    if plan is None:
        prepared = await prepared_uploads(body)
        tree = await engine._knowledge_tree(account, root)
        occupied = {row.get("uri") for row in tree.get("entries", [])}
        destinations = []
        for relative, content in prepared:
            target = child(record, parent + "/" + relative)
            candidate = PurePosixPath(relative)
            if target in occupied:
                for number in range(2, 10000):
                    renamed = candidate.with_name(f"{candidate.stem}-{number}{candidate.suffix}")
                    target = child(record, parent + "/" + str(renamed))
                    if target not in occupied:
                        break
                else:
                    raise HTTPException(409, "无法为重名文件生成新名称")
            occupied.add(target)
            destinations.append({"uri": target, "sha256": hashlib.sha256(content.encode()).hexdigest(),
                                 "content": content})
        plan = {"destinations": destinations, "written": []}
        await checkpoint(engine, plan)
    if len(plan["destinations"]) != len(body.get("files") or []):
        raise ValueError("上传检查点与受理文件不一致")
    directories = {parent}
    for target in plan["destinations"]:
        cursor = child(record, target["uri"]).rsplit("/", 1)[0]
        while cursor == parent or cursor.startswith(parent + "/"):
            directories.add(cursor)
            if cursor == parent:
                break
            cursor = cursor.rsplit("/", 1)[0]
    for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
        try:
            await request(engine, account, "POST", "/api/v1/fs/mkdir", json={"uri": directory})
        except HTTPException as exc:
            if exc.status_code != 409 and "ALREADY_EXISTS" not in str(exc.detail).upper():
                raise
    for item in plan["destinations"]:
        content = item["content"]
        target = item["uri"]
        if hashlib.sha256(content.encode()).hexdigest() != item["sha256"]:
            raise ValueError("上传文件与冻结检查点不一致")
        if any(row["uri"] == target for row in plan["written"]):
            continue
        try:
            result = await request(engine, account, "POST", "/api/v1/content/write", timeout=180,
                                   json={"uri": target, "content": content, "mode": "create", "wait": False})
        except HTTPException as exc:
            if exc.status_code != 409 and "ALREADY_EXISTS" not in str(exc.detail).upper():
                raise
            current = await request(engine, account, "GET", "/api/v1/content/read",
                                    params={"uri": target, "offset": 0, "limit": -1, "raw": "true"})
            if _content_text(current) != content:
                raise HTTPException(409, "文件上传时发生重名竞争，请刷新后重试") from exc
            result = {"replayed": True}
        plan["written"].append({"uri": target, "result": result})
        await checkpoint(engine, plan)
    return {"ok": True, "written": plan["written"], "file_count": len(plan["written"])}


async def execute(operation, body, engine):
    """Return the original source response shape after durable Worker acceptance."""
    repo = engine.repository
    miner, _ = settings_parts(engine.settings)
    account = str(miner["account_id"])
    if operation == "source.list":
        await asyncio.to_thread(repo.ensure_default_knowledge_source, account)
        records = [row for row in await asyncio.to_thread(repo.list_source_records) if row.get("account") == account]
        semaphore = asyncio.Semaphore(8)

        async def describe(record):
            async with semaphore:
                try:
                    return await engine._ov_source_detail(record)
                except Exception as exc:
                    item = source_inventory(record)
                    item.update(status="unavailable", error=str(exc), ingestion={"status": "failed", "stage": "failed",
                                "progress": 100, "error": "暂时无法检查 OpenViking 目录，请稍后重试",
                                "updated_at": utc_now(), "finished_at": utc_now()})
                    return item
        described = []
        for offset in range(0, len(records), 8):
            described.extend(await asyncio.gather(*(describe(row) for row in records[offset:offset + 8])))
        return {"accounts": [account], "current": account, "root_uri": "viking://resources",
                "sources": described,
                "skill_uri": SKILL_URI, "error": ""}
    if operation == "ov.tree":
        root = safe_uri(body.get("uri") or "viking://resources", allow_root=True)
        return {"account": account, **await engine._knowledge_tree(account, root)}
    if operation == "source.create":
        uri = safe_uri(body.get("uri"))
        if not (await engine._knowledge_tree(account, uri)).get("exists"):
            raise HTTPException(404, "所选 OpenViking 目录不存在")
        record = await asyncio.to_thread(repo.save_source, account, uri, name=str(body.get("name") or ""),
                                         tags=body.get("tags"), replace_tags=True)
        return {"ok": True, "source": await engine._ov_source_detail(record)}
    record = await asyncio.to_thread(repo.get_source, str(body.get("source_id") or ""))
    if record.get("account") != account:
        raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
    if operation == "source.update":
        record = await asyncio.to_thread(repo.update_source, record["source_id"],
                                         tags=body.get("tags"), name=body.get("name"))
        return {"ok": True, "source": await engine._ov_source_detail(record)}
    if operation == "source.remove":
        removed = await asyncio.to_thread(repo.remove_source, record["source_id"])
        return {"ok": True, "source_id": record["source_id"], "uri": removed["uri"]}
    if operation in {"source.detail", "source.tree"}:
        detail = await engine._ov_source_detail(record)
        return ({"source": detail} if operation == "source.detail" else
                {"source": {key: value for key, value in detail.items() if key != "tree"}, **detail["tree"]})
    if operation == "source.upload":
        return await upload(engine, record, body)
    target = child(record, body.get("uri"))
    if operation == "source.delete":
        result = await request(engine, account, "DELETE", "/api/v1/fs", timeout=180,
                               params={"uri": target, "recursive": "true", "wait": "false"})
        return {"deleted": True, "uri": target, "result": result}
    content = _content_text(await request(engine, account, "GET", "/api/v1/content/read",
                                          params={"uri": target, "offset": 0, "limit": -1, "raw": "true"}))
    if operation == "source.content":
        return {"source_id": record["source_id"], "uri": target, "name": target.rsplit("/", 1)[-1],
                "content": content, "editable": content_editable(target)}
    if operation != "source.save":
        raise ValueError("unsupported source operation")
    updated, original = body.get("content"), body.get("original_content")
    if not content_editable(target) or not isinstance(updated, str) or not isinstance(original, str):
        raise ValueError("该文件类型不支持编辑或文件内容不是文本")
    if len(updated.encode()) > 2 * 1024 * 1024:
        raise HTTPException(413, "知识源文件不能超过 2 MB")
    if content not in {original, updated}:
        raise HTTPException(409, "文件已被其他操作更新，请重新打开")
    result = await request(engine, account, "POST", "/api/v1/content/write",
                           json={"uri": target, "content": updated, "mode": "replace", "wait": False})
    return {"saved": True, "uri": target, "content": updated, "result": result}
