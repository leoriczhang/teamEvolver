from dataclasses import fields

from teamEvolver.config import (
    DEFAULT_LLM_API_BASE,
    DEFAULT_LLM_API_MODE,
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_QUEUE_CAPACITY,
    DEFAULT_LLM_TEMPERATURE,
    TeamEvolverConfig,
)
from teamEvolver.config_store import ConfigStore


def test_llm_defaults_have_one_runtime_definition(tmp_path):
    direct = TeamEvolverConfig()
    stored = ConfigStore(tmp_path / "missing.yaml").to_config()

    expected = {
        "llm_api_base": DEFAULT_LLM_API_BASE,
        "llm_model_id": DEFAULT_LLM_MODEL_ID,
        "llm_api_mode": DEFAULT_LLM_API_MODE,
        "llm_max_tokens": DEFAULT_LLM_MAX_TOKENS,
        "llm_temperature": DEFAULT_LLM_TEMPERATURE,
        "llm_max_concurrency": DEFAULT_LLM_MAX_CONCURRENCY,
        "llm_queue_capacity": DEFAULT_LLM_QUEUE_CAPACITY,
    }
    for name, value in expected.items():
        assert getattr(direct, name) == value
        assert getattr(stored, name) == value


def test_removed_llm_aliases_are_not_runtime_config_fields():
    names = {item.name for item in fields(TeamEvolverConfig)}

    assert "model_name" not in names
    assert "max_context_tokens" not in names
    assert "llm_provider" not in names
    assert not any(name.startswith("openrouter_") for name in names)
