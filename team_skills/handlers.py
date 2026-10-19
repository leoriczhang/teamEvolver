"""Worker handlers for Skill evolution, candidate validation, transfer and experience sync."""

from __future__ import annotations

from team_skills.evolution.candidate_pipeline import load_command, run_validation
from team_skills.evolution.cycle import run_cycle
from team_skills.evolution.model import model_for
from team_skills.evolution.prompt_context import prompt_context
from team_skills.evolution.prompt_test import run_stage_test
from team_skills.evolution.repository import EvolutionRepository
from team_skills.experiences.handler import sync
from team_skills.transfer.handler import transfer
from teamEvolver.runtime import HandlerResult


async def evolve(task, services):
    command = await load_command(task, services)
    payload = command["payload"]
    if command["result"] is not None:
        return HandlerResult.succeed(task.task_id)
    with prompt_context(payload.get("config", {})):
        operation = payload.get("operation")
        if operation == "tracing_test":
            import os

            import httpx

            from teamEvolver.security import SecretBox, SecretRepository
            raw = payload["tracing"]
            key = services.extras["config"].langfuse_tracing_secret_key
            if raw.get("secret_ref"):
                master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
                async with services.tx.tenant_tx("default") as conn:
                    key = await SecretRepository(services.tx, SecretBox(master)).resolve(
                        conn, secret_ref=raw["secret_ref"])
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(raw["host"].rstrip("/") + "/api/public/projects",
                                            auth=(raw.get("public_key", ""), key))
            result = {"ok": response.is_success, "error": "" if response.is_success else "connection failed"}
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await EvolutionRepository(services.tx).finish(conn, task.task_id, result)
            return HandlerResult.succeed(task.task_id)
        if operation == "ov_bootstrap":
            import asyncio

            from teamEvolver.admin.openviking_dirs import ensure_openviking_dirs
            result = await asyncio.to_thread(ensure_openviking_dirs, services.extras["config"],
                                             account_id=payload["account"])
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await EvolutionRepository(services.tx).finish(conn, task.task_id, {"openviking_dirs": result})
            return HandlerResult.succeed(task.task_id)
        if operation in {"prompt_test", "model_test"}:
            llm = await model_for(services, task.tenant_id, payload.get("model"))
            if operation == "prompt_test":
                result = await run_stage_test(payload["stage_id"], payload["session"],
                                              system_prompt=payload["prompt"], llm_factory=lambda: llm)
            else:
                import time
                started = time.monotonic()
                output = await llm.chat([{"role": "user", "content": "Reply OK."}], max_tokens=16)
                result = {"ok": True, "model": llm.model, "response": output,
                          "latency_ms": round((time.monotonic() - started) * 1000)}
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await EvolutionRepository(services.tx).finish(conn, task.task_id, result)
            return HandlerResult.succeed(task.task_id)
        return await run_cycle(task, services)


def register_handlers():
    """Fixed code registry; the composition root injects live collaborators."""
    return {
        "skill.evolve": evolve, "skill.validate": run_validation,
        "skill.transfer": transfer, "experience.sync": sync,
    }
