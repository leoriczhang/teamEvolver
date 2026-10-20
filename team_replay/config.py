"""Tenant Replay configuration, loaded only from the required PG module row."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REPLAY_CONFIG = {
    "concurrency": 2, "timeout_seconds": 600, "max_interactions": 4,
    "account_id": "default", "llm_model_id": "",
}


@dataclass(frozen=True)
class ReplayConfig:
    revision: int
    config: dict


async def load_replay_config(repo, conn):
    row = await repo.read_tenant_module(conn, "replay")
    forbidden = {"api_key", "token", "password", "llm_api_key"}
    if set(row.config) & forbidden:
        raise ValueError("Replay configuration must use secret refs")
    run_options({key: row.config[key] for key in ("concurrency", "timeout_seconds", "max_interactions")
                 if key in row.config})
    return ReplayConfig(row.revision, {**DEFAULT_REPLAY_CONFIG, **row.config})


def run_options(body):
    if not isinstance(body, dict):
        raise ValueError("Replay options must be an object")
    if set(body) - {"concurrency", "timeout_seconds", "max_interactions"}:
        raise ValueError("unsupported Replay option")
    options = {key: body.get(key, DEFAULT_REPLAY_CONFIG[key])
               for key in ("concurrency", "timeout_seconds", "max_interactions")}
    for key, low, high in (("concurrency", 1, 2), ("timeout_seconds", 30, 1800), ("max_interactions", 1, 20)):
        if type(options[key]) is not int or not low <= options[key] <= high:
            raise ValueError(f"{key} must be {low}..{high}")
    return options
