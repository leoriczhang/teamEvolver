"""Tenant-scoped Session dataset management and asynchronous True Replay."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from team_replay.datasets.batch import BatchCapacityError, DatasetBatchRunner, replay_snapshot
from team_replay.datasets.collections import (
    MAX_DATASET_BYTES,
    MAX_ITEMS,
    DatasetCollectionStore,
    DatasetConflict,
    DatasetNotFound,
    encode,
    item_summary,
    safe_id,
    snapshot_item,
)

from ..session_store import SessionStore
from ..tenants.registry import current_tenant_id


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_ids: list[str] | None = Field(default=None, max_length=MAX_ITEMS)
    filters: dict[str, str] | None = None


class CreateDataset(Selection):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class DatasetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=100000)
    requirements: list[str] = Field(min_length=1, max_length=100)


class BatchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concurrency: int = Field(default=2, ge=1, le=2)
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    max_interactions: int = Field(default=4, ge=1, le=20)


def register_dataset_routes(owner: Any, app: FastAPI) -> None:
    from .routes import _tenant_effective_config

    def evaluate(item: dict, options: dict) -> dict:
        from team_replay.hooks import ReplayUnsupported

        from ..replay_adapter import ensure_replay_host

        ensure_replay_host()
        try:
            return replay_snapshot(item, options)
        except ReplayUnsupported as exc:
            return {"ok": False, "status": "unsupported", "error": str(exc)}

    runner = DatasetBatchRunner(evaluate)
    owner._dataset_batch_runner = runner
    stores: dict[str, DatasetCollectionStore] = {}
    store_lock = asyncio.Lock()

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except DatasetNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except DatasetConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    async def store() -> DatasetCollectionStore:
        tenant_id = current_tenant_id()
        async with store_lock:
            if tenant_id not in stores:
                def build():
                    config = _tenant_effective_config(owner)
                    # Dataset/run writes are local-state traffic, never Viking.
                    local_config = replace(config, sharing_session_backend=(
                        "postgres" if config.storage_pg_enabled else "local"
                    ))
                    sessions = SessionStore.from_config(local_config, tenant_id=tenant_id)
                    result = DatasetCollectionStore(sessions._bucket, sessions._prefix)
                    result.recover(runner.owner_id)
                    return result
                stores[tenant_id] = await call(build)
            return stores[tenant_id]

    def selected_items(selection: Selection) -> list[dict]:
        from .routes import _filter_conversation_rows, _session_judge_score_index, _sort_conversation_rows

        if (selection.session_ids is None) == (selection.filters is None):
            raise ValueError("请指定 session_ids 或 filters 中的一种选择方式")
        config = _tenant_effective_config(owner)
        sessions = SessionStore.from_config(config, tenant_id=current_tenant_id())
        rows = sessions.list_conversations(limit=100000)
        if selection.session_ids is not None:
            ids = list(dict.fromkeys(safe_id(sid) for sid in selection.session_ids))
            if not ids:
                raise ValueError("请先选择 Session")
            by_id = {row["session_id"]: row for row in rows}
            if any(sid not in by_id for sid in ids):
                raise DatasetNotFound("部分 Session 不存在或不属于当前租户，请刷新后重试")
            rows = [by_id[sid] for sid in ids]
        else:
            filters = selection.filters or {}
            allowed = {"search", "status", "decision", "case", "skill", "start", "end", "sort_by", "order"}
            if set(filters) - allowed:
                raise ValueError("包含不支持的筛选条件")
            if filters.get("case"):
                scores = _session_judge_score_index(config)
                rows = [{**row, "judge": scores.get(row["session_id"]) or row.get("judge") or {}} for row in rows]
            rows = _filter_conversation_rows(
                rows, **{k: v for k, v in filters.items() if k not in {"sort_by", "order"}}
            )
            rows = _sort_conversation_rows(rows, sort_by=filters.get("sort_by", ""), order=filters.get("order", ""))
        if not rows:
            raise ValueError("当前筛选没有 Session")
        if len(rows) > MAX_ITEMS:
            raise ValueError(f"当前选择有 {len(rows)} 条，每个数据集最多 {MAX_ITEMS} 条，请缩小筛选范围")
        items, size = [], 0
        for row in rows:
            session = sessions.load_session(safe_id(row["session_id"]))
            if not session or not (session.get("turns") or session.get("messages")):
                raise ValueError(f"Session {row['session_id']} 正文不可用，未创建数据集")
            item = snapshot_item(session, row)
            size += len(encode(item))
            if size > MAX_DATASET_BYTES:
                raise ValueError("所选 Session 超过 64 MiB，请缩小范围")
            items.append(item)
        return items

    @app.get("/api/datasets")
    async def list_datasets(search: str = "", limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        repo = await store()
        rows = await call(repo.list)
        wanted = search.strip().casefold()
        rows = [row for row in rows if wanted in f"{row['name']} {row['description']}".casefold()]
        return {"datasets": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    @app.post("/api/datasets", status_code=201)
    async def create_dataset(body: CreateDataset):
        repo = await store()
        items = await call(selected_items, body)
        source = {
            "kind": "sessions", "filters": body.filters or {},
            "selection": "selected" if body.session_ids else "filtered",
        }
        return await call(repo.create, body.name, body.description, items, source)

    @app.get("/api/datasets/{dataset_id}")
    async def dataset_detail(dataset_id: str, search: str = "", limit: int = Query(20, ge=1, le=100),
                             offset: int = Query(0, ge=0)):
        repo = await store()
        dataset = await call(repo.load, dataset_id)
        rows = dataset.pop("items")
        dataset["item_count"] = len(rows)
        wanted = search.strip().casefold()
        rows = [item_summary(item) for item in rows if wanted in (
            f"{item['query']} {item['trace_id']} {item['session_id']} {item['title']}"
        ).casefold()]
        return {**dataset, "items": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}

    @app.patch("/api/datasets/{dataset_id}")
    async def update_dataset(dataset_id: str, body: DatasetUpdate):
        return await call((await store()).update, dataset_id, body.model_dump(exclude_none=True))

    @app.post("/api/datasets/{dataset_id}/items")
    async def add_items(dataset_id: str, body: Selection):
        repo = await store()
        await call(repo.metadata, dataset_id)
        await call(repo.assert_idle, dataset_id)
        items = await call(selected_items, body)
        return await call(repo.update, dataset_id, {}, items)

    @app.get("/api/datasets/{dataset_id}/items/{item_id}")
    async def item_detail(dataset_id: str, item_id: str):
        dataset = await call((await store()).load, dataset_id)
        item = next((item for item in dataset["items"] if item["item_id"] == item_id), None)
        if item is None:
            raise HTTPException(404, "数据集条目不存在")
        return item

    @app.patch("/api/datasets/{dataset_id}/items/{item_id}")
    async def update_item(dataset_id: str, item_id: str, body: ItemUpdate):
        return await call((await store()).change_item, dataset_id, item_id, body.model_dump())

    @app.delete("/api/datasets/{dataset_id}/items/{item_id}")
    async def delete_item(dataset_id: str, item_id: str):
        return await call((await store()).change_item, dataset_id, item_id, None)

    @app.delete("/api/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str):
        await call((await store()).delete, dataset_id)
        return {"deleted": True, "dataset_id": dataset_id}

    @app.get("/api/datasets/{dataset_id}/export")
    async def export_dataset(dataset_id: str):
        repo = await store()
        metadata = await call(repo.metadata, dataset_id)
        archive = await call(repo.export_zip, dataset_id)

        def chunks():
            try:
                while chunk := archive.read(64 * 1024):
                    yield chunk
            finally:
                archive.close()

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": (
                f"attachment; filename=dataset.zip; filename*=UTF-8''{quote(metadata['name'], safe='')}.zip"
            ),
            "X-Export-Count": str(metadata["item_count"]),
        })

    @app.get("/api/datasets/{dataset_id}/runs")
    async def list_runs(dataset_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        repo = await store()
        await call(repo.metadata, dataset_id)
        runs = await call(repo.runs, dataset_id)
        return {"runs": [{k: v for k, v in run.items() if k not in {"items", "owner_id"}}
                         for run in runs[offset:offset + limit]], "total": len(runs)}

    @app.post("/api/datasets/{dataset_id}/runs", status_code=202)
    async def start_run(dataset_id: str, body: BatchOptions):
        repo = await store()
        try:
            runner.reserve()
        except BatchCapacityError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
        try:
            run, items = await call(repo.create_run, dataset_id, body.model_dump(), runner.owner_id)
            owner._safe_create_task(runner.execute(repo, copy.deepcopy(run), items))
        except BaseException:
            runner.release()
            raise
        return {k: v for k, v in run.items() if k != "owner_id"}

    @app.get("/api/datasets/{dataset_id}/runs/{run_id}")
    async def run_detail(dataset_id: str, run_id: str, limit: int = Query(20, ge=1, le=100),
                         offset: int = Query(0, ge=0)):
        run = await call((await store()).run, dataset_id, run_id)
        run.pop("owner_id", None)
        run["items"] = run["items"][offset:offset + limit]
        return run

    @app.get("/api/datasets/{dataset_id}/runs/{run_id}/items/{item_id}")
    async def run_source_item(dataset_id: str, run_id: str, item_id: str):
        repo = await store()
        try:
            key = repo.run_key(dataset_id, run_id, "input.json")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        snapshot = await call(repo.read, key)
        presented = await call(
            repo.present_document,
            dataset_id,
            snapshot,
            run_id=run_id,
        )
        item = next(
            (
                item
                for item in presented["items"]
                if item["item_id"] == item_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(404, "重回放来源条目不存在")
        return item

    @app.post("/api/datasets/{dataset_id}/runs/{run_id}/cancel")
    async def cancel_run(dataset_id: str, run_id: str):
        run = await call((await store()).request_cancel, dataset_id, run_id)
        return {k: v for k, v in run.items() if k != "owner_id"}

    @app.get("/api/datasets/{dataset_id}/runs/{run_id}/results/{item_id}")
    async def result_detail(dataset_id: str, run_id: str, item_id: str):
        repo = await store()
        try:
            key = repo.run_key(dataset_id, run_id, f"results/{safe_id(item_id)}.json")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return await call(repo.read, key)
