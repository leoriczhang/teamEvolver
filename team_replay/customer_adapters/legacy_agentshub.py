"""Temporary one-turn compatibility bridge; requires explicit tenant binding."""
import os
from team_replay.adapters import LegacyAgentsHubHttpAdapter
from team_replay.factories import LegacyBranchFactory

REPLAY_ADAPTER = {"label": "Legacy AgentsHub (single turn)", "enabled": True}


def build_replay_adapter(config):
    endpoint = os.environ.get("AGENTSHUB_REPLAY_URL") or config.validation_agentshub_url
    api_key = os.environ.get("AGENTSHUB_REPLAY_API_KEY") or config.validation_agentshub_api_key
    return LegacyBranchFactory(
        adapter_builder=lambda: LegacyAgentsHubHttpAdapter(endpoint=endpoint, runtime_type="agentshub", api_key=api_key),
        isolated_sessions=os.environ.get("AGENTSHUB_REPLAY_ISOLATED", "") == "1",
    )
