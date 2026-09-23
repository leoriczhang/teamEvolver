"""Preinstalled isolated Hermes execution; bind explicitly for each tenant."""
from team_replay.local_hermes import LocalHermesFactory

REPLAY_ADAPTER = {"label": "Local Hermes (isolated)", "enabled": True}


def build_replay_adapter(config):
    return LocalHermesFactory({
        "base_url": config.llm_api_base, "api_key": config.llm_api_key,
        "model": config.llm_model_id or config.model_name,
        "api_mode": config.llm_api_mode, "max_tokens": config.llm_max_tokens,
    })
