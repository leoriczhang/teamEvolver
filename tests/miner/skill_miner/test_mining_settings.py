from __future__ import annotations

from copy import deepcopy

import pytest

from team_miner.skill_miner.knowledge_compiler import SKILL_BODY
from team_miner.skill_miner.ov_compile_mining import LEGACY_SKILL_BODY
from team_miner.skill_miner.settings import (
    LEGACY_COMPILER_WARNING,
    compiler_contract_version,
    compiler_prompt,
    reset_prompt,
    settings_payload,
    update_settings,
)


def test_settings_expose_only_effective_compile_controls():
    data = {"llm": {"api_key": "secret"}, "mining": {"pipeline": {"max_retries": 9}}}
    result = settings_payload(data)
    assert result["engine"] == "ov_compile"
    assert result["model"] == {"configuration_source": "openviking_server", "editable_here": False}
    assert result["pipeline"] == {"max_rounds": 3, "runtime_timeout_seconds": 3600}
    assert [entry["id"] for entry in result["prompts"]] == ["ov_compile"]
    assert result["candidate_contract_version"] == 2 and result["candidate_mode"] == "collection"
    assert not result["options_warnings"] and not result["candidate_mode_warning"]
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


@pytest.mark.parametrize(
    "custom", [LEGACY_SKILL_BODY, LEGACY_SKILL_BODY + "\nPreserve custom task boundaries.\n"],
    ids=["legacy-default", "custom-single"],
)
def test_saved_legacy_compiler_keeps_exact_content_and_reports_single_candidate_mode(custom):
    data = {"mining": {"prompts": {"ov_compile": custom}}}
    original = deepcopy(data)
    result = settings_payload(data)
    assert compiler_prompt(data) == custom and data == original
    assert compiler_contract_version(custom) == 1
    assert result["candidate_contract_version"] == 1 and result["candidate_mode"] == "single"
    assert result["candidate_mode_warning"] == LEGACY_COMPILER_WARNING
    assert result["options_warnings"] == [LEGACY_COMPILER_WARNING]
    assert result["prompts"][0]["effective_prompt"] == custom
    assert LEGACY_COMPILER_WARNING in result["prompts"][0]["description"]

    updated = update_settings(data, {"pipeline": {"max_rounds": 2}})
    assert compiler_prompt(data) == custom and updated["candidate_contract_version"] == 1
    reset = reset_prompt(data, "ov_compile")
    assert reset["candidate_contract_version"] == 2 and not reset["options_warnings"]
    assert compiler_prompt(data) == SKILL_BODY


def test_saving_custom_single_candidate_compiler_preserves_bytes_and_surfaces_mode():
    custom = "---\nname: custom-compiler\ndescription: Custom method compiler.\n---\nProduce one Skill Candidate.\n"
    data = {}
    result = update_settings(data, {"prompts": [{"id": "ov_compile", "prompt": custom}]})
    assert compiler_prompt(data) == custom
    assert data["mining"]["prompts"]["ov_compile"] == custom
    assert result["candidate_contract_version"] == 1 and result["options_warnings"] == [LEGACY_COMPILER_WARNING]


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
