"""Tests for the Prompt Studio (transparent skill-evolution pipeline).

Covers:
- default prompt resolution for all 5 LLM stages,
- override set/get/reset round-trip persisted to a temp file,
- effective_prompt honoring overrides and falling back byte-identically,
- shared-block expansion for skill-writing overrides,
- pipeline graph shape (nodes/edges, LLM nodes carry prompt ids),
- the live call sites (summarize/judge/execute) consulting effective_prompt, and
- the test-runner building the REAL per-stage user message and returning
  system + user + output (with a fake LLM, no network).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    """Point overrides at a temp file so tests never touch ~/.teamEvolver."""
    monkeypatch.setenv("TEAMEVOLVER_PROMPT_OVERRIDES_PATH", str(tmp_path / "prompt_overrides.json"))
    monkeypatch.setenv("TEAMEVOLVER_STAGE_SETTINGS_PATH", str(tmp_path / "stage_settings.json"))
    # Reload not required: prompt_studio reads the env var on each call.
    yield


def _ps():
    from teamEvolver.evolve import prompt_studio as ps

    return ps


def _session() -> dict:
    return {
        "session_id": "s-test-1",
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": "帮我整理接口调用流程并生成可复用步骤",
                "response_text": "整理完成，步骤如下 ...",
                "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}],
                "tool_results": [{"tool_call_id": "c1", "tool_name": "terminal", "content": "ok"}],
                "read_skills": [{"skill_name": "api-flow"}],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #
def test_all_stages_resolve_nonempty_defaults():
    ps = _ps()
    assert set(ps.STAGE_IDS) == {
        "summarize",
        "judge",
        "evolve_skill",
        "create_skill",
        "merge",
        "dataset_synthesis",
        "session_filter",
        "replay_checklist",
    }
    for stage_id in ps.STAGE_IDS:
        detail = ps.get_prompt(stage_id)
        assert detail["default_prompt"].strip(), stage_id
        assert detail["effective_prompt"] == detail["default_prompt"], stage_id
        assert detail["overridden"] is False, stage_id


def test_pipeline_graph_shape():
    ps = _ps()
    graph = ps.pipeline_graph()
    node_ids = {n["id"] for n in graph["nodes"]}
    # Core chain nodes are present.
    assert {"ingest", "summarize", "judge", "group", "evolve_skill", "create_skill", "validate", "publish"} <= node_ids
    llm_nodes = {n["id"]: n for n in graph["nodes"] if n["kind"] == "llm"}
    assert set(llm_nodes) == {
        "summarize",
        "judge",
        "evolve_skill",
        "create_skill",
        "merge",
        "dataset_synthesis",
        "session_filter",
        "replay_checklist",
    }
    for node in llm_nodes.values():
        assert node.get("prompt_id") in ps.STAGE_IDS
    # Edges reference known nodes.
    for edge in graph["edges"]:
        assert edge["from"] in node_ids and edge["to"] in node_ids


def test_override_set_get_reset_roundtrip():
    ps = _ps()
    ps.set_override("judge", "CUSTOM JUDGE PROMPT")
    detail = ps.get_prompt("judge")
    assert detail["overridden"] is True
    assert detail["effective_prompt"] == "CUSTOM JUDGE PROMPT"
    # effective_prompt returns the override, ignoring the passed fallback.
    assert ps.effective_prompt("judge", "IGNORED") == "CUSTOM JUDGE PROMPT"

    ps.reset_override("judge")
    assert ps.get_prompt("judge")["overridden"] is False
    # After reset, the fallback wins (byte-identical to the live default path).
    assert ps.effective_prompt("judge", "MY_FALLBACK") == "MY_FALLBACK"


def test_effective_prompt_expands_shared_blocks_for_overrides():
    ps = _ps()
    ps.set_override("create_skill", "HEAD __USER_OVERRIDE_RULE__ TAIL")
    eff = ps.effective_prompt("create_skill")
    assert "__USER_OVERRIDE_RULE__" not in eff
    assert "user-precedence" in eff.lower()


def test_empty_override_rejected():
    ps = _ps()
    with pytest.raises(ValueError):
        ps.set_override("summarize", "   ")


def test_stage_settings_override_live_call_options():
    ps = _ps()
    ps.set_stage_settings(
        "judge",
        {"model": "judge-model", "temperature": 0.6, "max_tokens": 4096},
    )

    detail = ps.get_prompt("judge")
    assert detail["settings_overridden"] is True
    assert detail["model"] == "judge-model"
    assert ps.stage_call_options("judge") == {
        "model": "judge-model",
        "temperature": 0.6,
        "max_tokens": 4096,
    }

    ps.reset_stage_settings("judge")
    assert ps.get_prompt("judge")["settings_overridden"] is False
    assert ps.stage_call_options("judge") == {
        "temperature": 0.1,
        "max_tokens": 32768,
    }


def test_unknown_stage_raises():
    ps = _ps()
    with pytest.raises(KeyError):
        ps.get_prompt("does-not-exist")


# --------------------------------------------------------------------------- #
# Live call sites consult effective_prompt                                     #
# --------------------------------------------------------------------------- #
def test_live_call_sites_use_override():
    ps = _ps()
    from teamEvolver.evolve.stages import execute, judge, summarize

    # No override -> byte-identical to module default.
    assert summarize._effective_summarize_system() == summarize._SUMMARIZE_SESSION_SYSTEM
    assert judge._effective_judge_system() == judge._JUDGE_SYSTEM
    assert execute._effective_system("merge", execute._MERGE_SKILL_SYSTEM) == execute._MERGE_SKILL_SYSTEM

    ps.set_override("summarize", "S-OVERRIDE")
    ps.set_override("merge", "M-OVERRIDE")
    assert summarize._effective_summarize_system() == "S-OVERRIDE"
    assert execute._effective_system("merge", execute._MERGE_SKILL_SYSTEM) == "M-OVERRIDE"


# --------------------------------------------------------------------------- #
# Test runner                                                                  #
# --------------------------------------------------------------------------- #
def test_build_stage_messages_summarize_and_judge():
    ps = _ps()
    session = _session()
    msgs = ps.build_stage_messages("summarize", session, system_prompt="SYS")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    payload = json.loads(msgs[1]["content"])
    assert payload["session_id"] == "s-test-1"
    assert payload["interactions"]

    msgs = ps.build_stage_messages("judge", session, system_prompt="SYS")
    payload = json.loads(msgs[1]["content"])
    # Judge payload carries the reconstructed trajectory.
    assert "trajectory" in payload
    assert payload["trajectory"]


def test_build_stage_messages_evolve_injects_skill_name():
    ps = _ps()
    session = _session()
    session["_probe_skill_name"] = "api-flow"
    msgs = ps.build_stage_messages(
        "evolve_skill", session, system_prompt="For skill {skill_name} do work"
    )
    assert "api-flow" in msgs[0]["content"]
    assert "{skill_name}" not in msgs[0]["content"]
    assert "Session evidence" in msgs[1]["content"]


def test_build_stage_messages_dataset_synthesis_uses_session_evidence():
    ps = _ps()
    msgs = ps.build_stage_messages(
        "dataset_synthesis",
        _session(),
        system_prompt=ps.get_prompt("dataset_synthesis")["default_prompt"],
    )

    payload = json.loads(msgs[1]["content"])
    assert payload["sessions"][0]["session_id"] == "s-test-1"
    assert payload["candidate_skill"]["content"]
    assert "{case_count}" not in msgs[0]["content"]


@pytest.mark.anyio
async def test_run_stage_test_returns_system_user_output():
    ps = _ps()
    captured = {}

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return '{"task_completion": 1.0}'

    result = await ps.run_stage_test(
        "judge",
        _session(),
        system_prompt="MY TEST JUDGE PROMPT",
        llm_factory=lambda: FakeLLM(),
    )
    assert result["stage_id"] == "judge"
    assert result["system_prompt"] == "MY TEST JUDGE PROMPT"
    assert result["user_message"]
    assert result["output"] == '{"task_completion": 1.0}'
    # The temperature for judge was passed through.
    assert captured["kwargs"].get("temperature") == 0.1
