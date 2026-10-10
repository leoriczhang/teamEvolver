from __future__ import annotations

from copy import deepcopy

import pytest

from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.settings import compiler_prompt, reset_prompt, settings_payload, update_settings


def test_settings_expose_only_effective_compile_controls():
    data = {"llm": {"api_key": "secret"}, "mining": {"pipeline": {"max_retries": 9}}}
    result = settings_payload(data)
    assert result["engine"] == "ov_compile"
    assert result["model"] == {"configuration_source": "openviking_server", "editable_here": False}
    assert result["pipeline"] == {"max_rounds": 3, "runtime_timeout_seconds": 3600}
    assert [entry["id"] for entry in result["prompts"]] == ["ov_compile"]
    assert "secret" not in str(result)


def test_saved_compiler_is_the_body_used_by_future_rounds():
    data = {"mining": {"pipeline": {"max_retries": 9}, "prompts": {"sample_package": "obsolete"}}}
    custom = SKILL_BODY + "\nAlways explain conflicting Evidence.\n"
    result = update_settings(data, {
        "pipeline": {"max_rounds": 2, "runtime_timeout_seconds": 600},
        "prompts": [{"id": "ov_compile", "prompt": custom}],
    })
    assert compiler_prompt(data) == custom
    assert result["prompts"][0]["overridden"] is True
    assert data["mining"] == {
        "pipeline": {"max_rounds": 2, "runtime_timeout_seconds": 600},
        "prompts": {"ov_compile": custom},
    }
    reset_prompt(data, "ov_compile")
    assert compiler_prompt(data) == SKILL_BODY
    assert settings_payload(data)["pipeline"]["max_rounds"] == 2


@pytest.mark.parametrize("body", [
    {"pipeline": {"max_rounds": 6}},
    {"pipeline": {"max_rounds": True}},
    {"pipeline": {"runtime_timeout_seconds": 59}},
    {"pipeline": {"max_retries": 1}},
    {"prompts": [{"id": "sample_package", "prompt": "obsolete"}]},
    {"prompts": [{"id": "ov_compile", "prompt": "invalid compiler"}]},
])
def test_invalid_setting_never_partially_mutates_config(body):
    data = {"mining": {"pipeline": {"max_rounds": 3}}, "team": {"name": "team"}}
    original = deepcopy(data)
    with pytest.raises(ValueError):
        update_settings(data, body)
    assert data == original
