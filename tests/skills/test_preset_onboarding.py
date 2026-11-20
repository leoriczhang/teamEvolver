"""A preset baseline must agree with its original business request before registration."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/te-agent-evolution-setup/scripts/prepare_profile.py"
spec = importlib.util.spec_from_file_location("prepare_business_profile", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_original_request_checks_roles_variables_and_public_history():
    preset = {
        "systemPrompt": "{{ rule }}",
        "userPromptTemplate": "判断：{{q}}",
        "variables": [{"name": "rule", "defaultValue": "基线默认"}],
    }
    history = [{"role": "assistant", "content": "C：上文"}]
    sample = {
        "variables": {"rule": "本次规则", "q": "内容"},
        "input_messages": history,
        "message_prefixes": {"assistant": "C：", "user": "HWP："},
        "current_user_message": "判断：内容",
        "final_request": {
            "model": "actual",
            "messages": [
                {"role": "system", "content": "本次规则"},
                *history,
                {"role": "user", "content": "HWP：判断：内容"},
            ],
        },
    }
    args = (preset, sample, "profile", "agent", "execution-user", "runtime")
    profile = helper.prepare(*args)
    assert profile["environment"]["template_variables"] == {"rule": "基线默认"}
    assert profile["environment"]["request"] == {"model": "actual"}
    assert profile["components"][0]["content"] == "{{ rule }}"
    sample["final_request"]["messages"][-1]["role"] = "assistant"
    with pytest.raises(ValueError, match="final request messages"):
        helper.prepare(*args)
    del preset["variables"]
    with pytest.raises(ValueError, match="defaults"):
        helper.prepare(*args)
