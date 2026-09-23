"""Bounded background execution for saved Session collections."""
from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Callable

from .collections import DatasetCollectionStore

logger = logging.getLogger(__name__)


class BatchCapacityError(RuntimeError):
    pass


class DatasetBatchRunner:
    """At most two batches and four runtime/judge threads per server."""

    def __init__(self, evaluate: Callable) -> None:
        self.evaluate = evaluate
        self.owner_id = uuid.uuid4().hex
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dataset-replay")
        self.semaphore = asyncio.Semaphore(4)
        self.active = 0
        self.closing = False

    def reserve(self) -> None:
        if self.closing or self.active >= 2:
            raise BatchCapacityError("批量重回放繁忙，最多同时执行两个批次，请稍后重试")
        self.active += 1

    def release(self) -> None:
        self.active -= 1

    async def execute(self, store: DatasetCollectionStore, run: dict, items: list[dict]) -> None:
        dataset_id, run_id = run["dataset_id"], run["run_id"]
        pending = iter(items)

        async def worker() -> None:
            for item in pending:
                if self.closing:
                    return
                if not await asyncio.to_thread(store.item_running, dataset_id, run_id, item["item_id"]):
                    return
                try:
                    async with self.semaphore:
                        call = partial(self.evaluate, copy.deepcopy(item), dict(run["options"]))
                        result = await asyncio.get_running_loop().run_in_executor(
                            self.executor, copy_context().run, call,
                        )
                except Exception as exc:
                    logger.exception("Dataset replay item failed: %s", item["item_id"])
                    result = {"ok": False, "status": "failed", "error": str(exc)}
                await asyncio.to_thread(store.finish_item, dataset_id, run_id, item["item_id"], result)

        workers = [asyncio.create_task(worker()) for _ in range(run["options"]["concurrency"])]
        try:
            await asyncio.gather(*workers)
            await asyncio.to_thread(store.finish_run, dataset_id, run_id, interrupted=self.closing)
        except BaseException as exc:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await asyncio.to_thread(store.finish_run, dataset_id, run_id, interrupted=True,
                                    error=str(exc) or "服务关闭，批次中断")
            raise
        finally:
            self.release()

    def stop(self) -> None:
        self.closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)


def replay_snapshot(item: dict, options: dict) -> dict:
    """Execute a saved query through the existing independent runtime and judge."""
    from ..engine import make_context, read_team_evolver_harness, resolve_factory
    from ..execution import run_branch
    from ..hooks import ReplayUnsupported

    source = item["session"]
    if source.get("source") == "managed_agent_candidate_audit" or (
        source.get("runtime_context") or {}
    ).get("candidate_job_id"):
        raise ReplayUnsupported("候选审计 Session 不能作为重回放来源")
    case = {
        "query": item["query"], "requirements": item["requirements"],
        "turn_num": item["turn_num"], "session_id": item["session_id"],
        "progressive_disclosure": {"batch_size": 4},
        "materials": source.get("materials") or [],
    }
    factory = resolve_factory(source)
    # No candidate Skill is injected for a Session replay. The tenant factory
    # restores the recorded context and opens an independent runtime session.
    context = make_context("baseline", None, case, source, options["timeout_seconds"])
    return run_branch(factory, context, case, harness=read_team_evolver_harness(),
                      max_interactions=options["max_interactions"])
