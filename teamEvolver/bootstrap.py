"""Read-only process bootstrap; mutable settings belong to PostgreSQL."""
from __future__ import annotations

import os
from dataclasses import asdict, fields
from pathlib import Path

import yaml

from .config import TeamEvolverConfig
from .database.pool import dsn_from_env

ENV_FIELDS = {
    "TEAMEVOLVER_PG_DSN": "storage_pg_dsn",
    "TEAMEVOLVER_PG_SCHEMA": "storage_pg_schema",
    "TEAMEVOLVER_LLM_API_KEY": "llm_api_key",
    "TEAMEVOLVER_LLM_BASE_URL": "llm_api_base",
    "TEAMEVOLVER_LLM_MODEL": "llm_model_id",
    "TEAMEVOLVER_LLM_CONCURRENCY": "llm_max_concurrency",
    "TEAMEVOLVER_LLM_QUEUE_CAPACITY": "llm_queue_capacity",
    "TEAMEVOLVER_OV_ENDPOINT": "sharing_viking_endpoint",
    "TEAMEVOLVER_OV_API_KEY": "sharing_viking_api_key",
    "TEAMEVOLVER_TENANT_TOKEN": "tenant_machine_token",
    "TEAMEVOLVER_SELF_REGISTRATION_ENABLED": "self_registration_enabled",
    "TEAMEVOLVER_API_PORT": "proxy_port",
    "TEAMEVOLVER_API_HOST": "proxy_host",
}


def load_bootstrap(*, path: Path | None = None, environ=None) -> TeamEvolverConfig:
    """Load flat current field names, rejecting former section/dot-path YAML."""
    env = os.environ if environ is None else environ
    raw_path = env.get("TEAMEVOLVER_CONFIG_FILE", "")
    path = path or (Path(raw_path).expanduser() if raw_path else None)
    data = {}
    if path is not None:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("bootstrap configuration could not be read") from exc
        if not isinstance(data, dict):
            raise ValueError("bootstrap configuration must be an object")
    defaults = asdict(TeamEvolverConfig())
    unknown = set(data) - set(defaults)
    if unknown:
        raise ValueError("unsupported bootstrap fields: " + ", ".join(sorted(unknown)))
    for name, field in ENV_FIELDS.items():
        if name in env:
            data[field] = env[name]
    for field in fields(TeamEvolverConfig):
        if field.name not in data:
            continue
        value, default = data[field.name], defaults[field.name]
        if type(default) is bool:
            if value not in (True, False, "true", "false", "1", "0"):
                raise ValueError(f"{field.name} must be a boolean")
            data[field.name] = value is True or value in ("true", "1")
        elif type(default) is int:
            data[field.name] = int(value)
        elif type(default) is float:
            data[field.name] = float(value)
        elif isinstance(default, dict) and not isinstance(value, dict):
            raise ValueError(f"{field.name} must be an object")
        elif isinstance(default, list) and not isinstance(value, list):
            raise ValueError(f"{field.name} must be a list")
    data["_config_file"] = str(path or "")
    if not data.get("storage_pg_dsn"):
        data["storage_pg_dsn"] = dsn_from_env() if environ is None else ""
    if data.get("storage_pg_dsn"):
        data["storage_pg_enabled"] = True
    return TeamEvolverConfig(**data)
