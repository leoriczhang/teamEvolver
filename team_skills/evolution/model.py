"""Resolve a pinned model configuration and secret only inside Worker."""
from __future__ import annotations

import os

from teamEvolver.llm import AsyncLLMClient
from teamEvolver.security import SecretBox, SecretRepository


async def model_settings(services, tenant, model=None):
    if model is None:
        async with services.tx.tenant_tx(tenant) as conn:
            row = await services.extras["config_repo"].read_tenant_module(conn, "skills")
        model = row.config.get("model", {})
    config = services.extras["config"]
    key = config.llm_api_key
    if model.get("secret_ref"):
        master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
        async with services.tx.tenant_tx(tenant) as conn:
            key = await SecretRepository(services.tx, SecretBox(master)).resolve(conn, secret_ref=model["secret_ref"])
    return {
        "api_key": key, "base_url": model.get("base_url", config.llm_api_base),
        "model": model.get("model", config.llm_model_id), "max_tokens": model.get("max_tokens", config.llm_max_tokens),
        "temperature": model.get("temperature", config.llm_temperature),
    }


async def model_for(services, tenant, model=None):
    settings = await model_settings(services, tenant, model)
    from .prompt_context import settings as prompt_settings
    stage_settings = (prompt_settings() or {}).get("stage_settings", {})
    resolved = {}
    for stage_id, raw in stage_settings.items():
        if raw.get("secret_ref"):
            value = await model_settings(services, tenant, raw)
            resolved[stage_id] = value["api_key"]
    return AsyncLLMClient(
        **settings, tenant_id=tenant, stage_api_keys=resolved,
    )
