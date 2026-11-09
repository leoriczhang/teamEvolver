"""Injected host construction from a fixed Replay adapter and model configuration."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from ._util import stable_hash
from .adapters.loader import build_from_content
from .adapters.repository import ReplayAdapterRepository
from .evaluation import external_evaluation_enabled, normalize_evaluation
from .evaluator_adapters import EvaluatorAdapterRepository, build_evaluator
from .external_agent import evaluate_external_http


class FixedHost:
    def __init__(self, factory, harness, *, evaluation=None, evaluation_secret="", evaluator=None):
        self.factory = factory
        self.harness = harness
        self.evaluation = copy.deepcopy(evaluation or {})
        self._evaluation_secret = evaluation_secret
        self._evaluator = evaluator

    def resolve_replay_factory(self, runtime_type, source_session):
        return self.factory

    def judge_harness(self):
        return copy.deepcopy(self.harness)

    def evaluate_external(self, request):
        if self._evaluator is not None:
            return self._evaluator.evaluate(request)
        return evaluate_external_http(request, self.evaluation["external_agent"], self._evaluation_secret)


async def host_for(payload, services, tenant_id):
    injected = services.extras.get("replay_host_factory")
    if injected:
        return injected(copy.deepcopy(payload), tenant_id)
    adapter = payload["adapter"]
    if not adapter:
        raise ValueError("Replay adapter is not configured")
    async with services.tx.tenant_tx(tenant_id) as conn:
        source = await ReplayAdapterRepository(services.tx).source(conn, adapter["adapter_id"], adapter["revision"])
    if stable_hash(source["source"]) != adapter["source_hash"]:
        raise ValueError("Replay adapter source hash mismatch")
    values = copy.deepcopy(payload["config"])
    evaluation = normalize_evaluation(values.get("evaluation"))
    evaluation_secret = ""
    evaluator = None
    if external_evaluation_enabled(evaluation):
        reference = evaluation["external_agent"].get("secret_ref")
        if reference:
            secrets = services.extras.get("secret_repo")
            if secrets is None:
                raise ValueError("External evaluation Secret Ref resolver is unavailable")
            try:
                async with services.tx.tenant_tx(tenant_id) as conn:
                    evaluation_secret = await secrets.resolve(conn, secret_ref=reference)
            except (KeyError, ValueError) as exc:
                raise ValueError("External evaluation credential is unavailable") from exc
        reference = evaluation["external_agent"].get("adapter")
        if reference:
            async with services.tx.tenant_tx(tenant_id) as conn:
                evaluator_source = await EvaluatorAdapterRepository(services.tx).require(conn, reference)
            evaluator = await asyncio.to_thread(
                build_evaluator, evaluator_source["source"], reference["file"], evaluation["external_agent"],
                tenant_id=tenant_id, secret=evaluation_secret,
            )
    agent_lab = values.get("agent_lab")
    if agent_lab and agent_lab.get("secret_ref"):
        secrets = services.extras.get("secret_repo")
        if secrets is None:
            raise ValueError("Replay control Secret Ref resolver is unavailable")
        async with services.tx.tenant_tx(tenant_id) as conn:
            agent_lab["control_secret"] = await secrets.resolve(conn, secret_ref=agent_lab["secret_ref"])
    config = SimpleNamespace(**values)
    factory = await asyncio.to_thread(
        build_from_content, source["source"], adapter["adapter_id"], config, tenant_id=tenant_id,
    )
    provider = services.extras.get("replay_judge_harness")
    if provider:
        harness = provider(payload["config"], tenant_id)
    elif not services.extras.get("config_repo"):
        harness = {}
    else:
        from team_skills.evolution.model import model_settings
        harness = await model_settings(services, tenant_id)
    return FixedHost(factory, harness, evaluation=evaluation, evaluation_secret=evaluation_secret, evaluator=evaluator)
