"""Injected host construction from a fixed Replay adapter and model configuration."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from ._util import stable_hash
from .adapters.loader import build_from_content
from .adapters.repository import ReplayAdapterRepository


class FixedHost:
    def __init__(self, factory, harness):
        self.factory = factory
        self.harness = harness

    def resolve_replay_factory(self, runtime_type, source_session):
        return self.factory

    def judge_harness(self):
        return copy.deepcopy(self.harness)


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
    config = SimpleNamespace(**payload["config"])
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
    return FixedHost(factory, harness)
