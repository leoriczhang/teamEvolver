"""Revisioned Replay evaluation selection; execution stays in the existing Worker."""
from __future__ import annotations

import copy

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from teamEvolver.config_runtime import ConfigRevisionConflict
from teamEvolver.tenants.registry import current_tenant_id

from ..config import load_replay_config
from ..evaluation import normalize_evaluation
from ..evaluator_adapters import EvaluatorAdapterRepository
from .common import ERRORS, actor, body, error


def create_router(services):
    router = APIRouter(tags=["replay"])
    configs, tx = services.extras["config_repo"], services.tx

    @router.get("/api/replay-evaluation")
    async def describe():
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                row = await load_replay_config(configs, conn)
            return {"revision": row.revision, "evaluation": row.config["evaluation"]}
        except ERRORS as exc:
            raise error(exc) from exc

    @router.put("/api/replay-evaluation")
    async def save(request: Request):
        actor(request)
        raw = await body(request)
        if set(raw) != {"expected_revision", "evaluation"}:
            raise HTTPException(400, "expected evaluation and expected_revision")
        expected = raw["expected_revision"]
        if type(expected) is not int or expected < 1:
            raise HTTPException(400, "expected_revision must be a positive integer")
        try:
            evaluation = normalize_evaluation(raw["evaluation"])
            async with tx.tenant_tx(current_tenant_id()) as conn:
                adapter = evaluation["external_agent"].get("adapter")
                if adapter:
                    await EvaluatorAdapterRepository(tx).require(conn, adapter)
                row = await configs.read_tenant_module(conn, "replay")
                value = {**copy.deepcopy(row.config), "evaluation": evaluation}
                revision = await configs.write_tenant_module(
                    conn, module="replay", expected_revision=expected, config=value,
                )
            return {"revision": revision, "evaluation": evaluation}
        except ConfigRevisionConflict as exc:
            raise HTTPException(409, "Replay configuration changed; reload before saving") from exc
        except ERRORS as exc:
            raise error(exc) from exc

    return router
