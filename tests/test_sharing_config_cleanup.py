from dataclasses import fields

import pytest
import yaml

from teamEvolver.admin.identity_store import registry_path
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from team_skills.evolution.kernel.settings import EvolveServerConfig


REMOVED_SHARING_KEYS = {
    "auto_pull_on_start",
    "backend",
    "enabled",
    "endpoint",
    "push_min_effectiveness",
    "push_min_injections",
    "session_upload_interval",
    "skill_reload_interval_seconds",
    "skill_reload_mode",
    "user_alias",
    "viking_agent_id",
    "viking_customer_id",
    "viking_group_id",
    "viking_personal_api_key",
    "viking_personal_api_keys",
    "viking_personal_user",
    "viking_resources_api_key",
    "viking_team_api_key",
    "viking_user_api_key",
}


def test_removed_sharing_keys_are_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "users_registry_path": "/legacy/users.json",
                "sharing": {
                    **{key: "legacy" for key in REMOVED_SHARING_KEYS},
                    "skill_reload_mode": "callback",
                    "skill_reload_interval_seconds": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    store = ConfigStore(path)
    with pytest.raises(ValueError, match="unsupported configuration"):
        store.load()
    assert REMOVED_SHARING_KEYS.isdisjoint(
        item.name.removeprefix("sharing_")
        for item in fields(TeamEvolverConfig)
    )
    assert {"viking_customer_id", "viking_group_id"}.isdisjoint(
        item.name for item in fields(EvolveServerConfig)
    )
    assert {
        "sharing_backend",
        "sharing_endpoint",
        "sharing_user_alias",
        "sharing_viking_personal_user",
    }.isdisjoint(item.name for item in fields(TeamEvolverConfig))

    with pytest.raises(ValueError, match="unsupported configuration"):
        store.save(
            {
                "users_registry_path": "/legacy/users.json",
                "sharing": {key: "legacy" for key in REMOVED_SHARING_KEYS},
            }
        )


def test_removed_runtime_sections_and_datasource_keys_are_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    with pytest.raises(ValueError, match="unsupported configuration"):
        store.save({
            "agent_protocol": {"identity_mode": "dual"},
            "skills": {"delivery_mode": "push"},
            "llm_provider": "openrouter",
            "max_context_tokens": 123,
            "mining": {"pipeline": {"max_rounds": 3}},
            "model_name": "old-model",
            "openrouter": {"route": "fallback"},
            "proxy": {"port": 9999},
            "sharing_backend": "viking",
            "aggregation": {
                "insight_skill_uri": "viking://agent/skills/unused",
                "key_seed": "legacy",
                "max_users_per_batch": 12,
            },
            "dreamcycle": {
                "active_start_hour": 0,
                "enabled": True,
                "job_prompts": {"cleanup": "old"},
            },
            "evolve": {
                "batch_size": 20,
                "reject_rewrite": True,
                "use_success_feedback": True,
            },
            "llm": {"provider": "openrouter"},
            "langfuse": {
                "enabled": True,
                "host": "https://old.example",
                "public_key": "old",
                "secret_key": "old",
                "mappers": [{"name": "old"}],
                "tracing_enabled": True,
            },
            "datasource": {
                "type": "skillopt",
                "options": {"token": "old"},
                "legacy_converter_code": "old",
                "adapter": "tenant.py",
                "schedule": {"max_sessions": 1000, "enabled": False},
            },
        })


def test_local_identity_paths_follow_config_directory(tmp_path):
    config = TeamEvolverConfig(_config_file=str(tmp_path / "config.yaml"))

    assert registry_path(config) == tmp_path / "console_identity.sqlite3"
    assert "users_registry_path" not in {
        item.name for item in fields(TeamEvolverConfig)
    }
