"""Replay Worker: saved inputs, fixed adapter, fenced checkpoints and outcomes."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

from teamEvolver.runtime import HandlerResult

from .adapters.guard import GuardedFactory
from .datasets.synthesis_handler import synthesize
from .engine import make_context, treatment_for_case
from .execution import run_branch
from .hooks import validate_observation
from .integration import host_for
from .repository import ReplayRepository
from .runner import aggregate_results, evaluate_case
from .schemas import spec_from_dict


async def current(task, services):
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        row = await services.tasks.get(conn, task_id=task.task_id)
        if row["cancel_requested"]:
            return False
        await services.tasks.require_current(conn, task)
    return True


async def run(task, services):
    domain = task.subject_type.removesuffix("_replay")
    repo = ReplayRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        record = await repo.get(conn, domain, task.payload_ref)
        outputs = await repo.cases(conn, domain, task.payload_ref)
    if record["input_hash"] != task.payload_hash:
        return HandlerResult.fail("replay_input_hash_mismatch")
    if record["result"] is not None:
        return HandlerResult.succeed(task.task_id)
    payload = record["input"]
    if payload["operation"] == "adapter_test":
        return await test_adapter(task, services, repo, payload)
    if payload["operation"] == "synthesis":
        return await synthesize(task, services, repo, payload)
    try:
        host = await host_for(payload, services, task.tenant_id)
    except ValueError:
        return HandlerResult.fail("replay_adapter_unavailable")
    options = payload["options"]
    spec = spec_from_dict(payload["spec"]) if payload["spec"] else None
    cases = list(spec.cases) if spec else payload["dataset"]["execution_document"]["cases"]
    owner = f"{task.owner_id}:{task.task_id}:{task.fence}"
    slot = None
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        for index in range(int(payload["config"].get("concurrency", 2))):
            key = f"replay:slot:{index}"
            fence = await services.leases.acquire(conn, tenant_id=task.tenant_id, resource_key=key,
                                                  owner_id=owner, lease_seconds=60)
            if fence is not None:
                slot = (key, fence)
                break
    if slot is None:
        return HandlerResult.retry("replay_capacity", backoff_seconds=5)
    factory = host.resolve_replay_factory(str((spec.runtime if spec else {}).get("type", "")), {})
    factory = GuardedFactory(factory, task, services, slot, owner)
    judge = services.extras.get("replay_judge")
    feedback = services.extras.get("replay_feedback")
    try:
        semaphore = asyncio.Semaphore(options.get("concurrency", 1))
        async def execute(case):
            async with semaphore:
                return await execute_one(case)

        async def execute_one(case):
            if case["case_id"] in outputs:
                return True
            if not await current(task, services):
                return False
            def evaluate():
                try:
                    if spec:
                        return evaluate_case(spec, copy.deepcopy(case), host=host, factory=factory,
                                             judge=judge, feedback=feedback)
                    source = copy.deepcopy(case.get("provenance", {}).get("session_snapshot") or {})
                    source["runtime"] = case.get("provenance", {}).get("source_runtime") or source.get("runtime") or {}
                    treatment = treatment_for_case(payload["dataset"].get("skill_treatment"), case)
                    context = make_context("baseline", treatment, case, source, options["timeout_seconds"],
                                           snapshot=case.get("provenance", {}).get("context_snapshot") or {})
                    call = {}
                    if judge:
                        call["judge"] = judge
                    if feedback:
                        call["feedback"] = feedback
                    return run_branch(factory, context, case, harness=host.judge_harness(),
                                      max_interactions=options["max_interactions"], **call)
                except Exception:
                    return {"status": "failed", "ok": False, "completed": False, "accepted": False,
                            "verdict": "inconclusive", "reason": "Replay execution unavailable"}
            work = asyncio.create_task(asyncio.to_thread(evaluate))
            while not work.done():
                await asyncio.wait({work}, timeout=0.25)
                if not await current(task, services):
                    return False
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    if not await services.leases.heartbeat(
                        conn, tenant_id=task.tenant_id, resource_key=slot[0], owner_id=owner,
                        fence=slot[1], lease_seconds=60,
                    ):
                        raise RuntimeError("Replay resource lease lost")
            result = work.result()
            if not await current(task, services):
                return False
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await repo.save_case(conn, tenant_id=task.tenant_id, domain=domain, run_id=task.task_id,
                                     case=case, result=result)
            outputs[case["case_id"]] = result
            return True
        results = await asyncio.gather(*(execute(case) for case in cases))
        if not all(results):
            return HandlerResult.cancelled()
        if spec:
            result = next(iter(outputs.values())) if len(outputs) == 1 else aggregate_results(
                [(case["case_id"], outputs[case["case_id"]]) for case in cases],
            )
        else:
            result = {"status": "completed", "total": len(cases), "completed": len(outputs),
                      "succeeded": sum(r.get("completed") is True for r in outputs.values()),
                      "failed": sum(not r.get("ok") or not r.get("completed") for r in outputs.values()),
                      "cases": outputs}
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await repo.finish(conn, domain, task.task_id, result)
        return HandlerResult.succeed(task.task_id)
    finally:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.leases.release(conn, tenant_id=task.tenant_id, resource_key=slot[0],
                                          owner_id=owner, fence=slot[1])


def register_handlers():
    return {"replay.run": run, "dataset.run": run}


async def test_adapter(task, services, repo, payload):
    raw = payload["options"]["test"]
    context = raw["context"]
    test_host = await host_for({"config": payload["config"], "adapter": raw}, services, task.tenant_id)
    factory = test_host.resolve_replay_factory("", {})
    guarded = GuardedFactory(factory, task, services, None, "")
    def test():
        replay_context = make_context(
            "baseline", context.get("skill"), {"materials": context.get("materials", [])},
            {"runtime": {"type": context["runtime_type"]}}, min(120, int(context.get("timeout_seconds", 30))),
            snapshot=context.get("context_snapshot", {}),
        )
        session = guarded.open(replay_context)
        try:
            return {"observation": asdict(validate_observation(session.send(raw["query"])))}
        finally:
            session.close()
    result = await asyncio.to_thread(test)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.finish(conn, "skill", task.task_id, result)
    return HandlerResult.succeed(task.task_id)
