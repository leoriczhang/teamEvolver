import pytest

from team_ma.discovery import replay_capability
from team_skills.evolution.session_processing import evidence_gap
from teamEvolver.managed_agents.events import child_threads, stable_end, turns


def test_mounts_are_not_skill_usage_and_children_stay_with_parent():
    events = [
        {"id": "u", "type": "user.message", "content": [{"type": "text", "text": "write a note"}]},
        {"id": "c", "type": "agent.message", "session_thread_id": "writer",
         "content": [{"type": "text", "text": "child response"}]},
        {"id": "t", "type": "agent.tool_use", "name": "read", "input": {"path": "/skills/skl-1/SKILL.md"}},
        {"id": "a", "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
    ]
    result = turns(events, {"skl-1": "note-craft", "skl-2": "never-read"})
    assert len(result) == 1
    assert result[0]["response_text"] == "done"
    assert result[0]["used_skills"] == ["note-craft"]
    assert result[0]["skill_refs"][0]["event_id"] == "t"
    assert stable_end(events) is None


def test_two_ranges_and_two_splits_do_not_fake_independent_ma_sessions():
    def session(ident, parent):
        return {"session_id": ident, "user_alias": "one-user",
                "runtime": {"type": "managed-agents", "connection_id": "connection"},
                "runtime_context": {"source_session_id": parent}}
    policy = {"min_group_sessions": 2, "min_group_users": 1}
    assert evidence_gap([session("range1", "parent"), session("range2-split", "parent")], policy)
    assert evidence_gap([session("range1", "parent1"), session("range2", "parent2")], policy) == ""


def test_isolation_failures_block_comparability():
    config = {"replay_enabled": True, "replay_vault_ids": []}
    graph = {"agents": {"arbitrary": {"memory": {"store": "production"}}}}
    result = replay_capability(config, graph, {"config": {"type": "cloud"}})
    assert not result["supported"]
    assert "Memory" in result["reason"]
    assert not replay_capability(config, {"agents": {}}, {"config": {"type": "self_hosted"}})["supported"]


@pytest.mark.parametrize("agent_fields,config_fields,reason", [
    ({"memory_store_id": "production"}, {}, "Memory"),
    ({"mcp_servers": [{"url": "https://mcp.example"}]}, {}, "MCP"),
])
def test_custom_declarations_do_not_bypass_other_isolation_gates(agent_fields, config_fields, reason):
    config = {"replay_enabled": True, **config_fields}
    graph = {"agents": {"root": {"tools": [{"type": "custom", "name": "render_video"}], **agent_fields}}}
    result = replay_capability(config, graph, {"id": "replay", "config": {"type": "cloud"}})
    assert not result["supported"] and reason in result["reason"]


def test_root_thread_list_entry_is_not_merged_as_a_child():
    main = [{"id": "running", "type": "session.thread_status_running", "session_thread_id": "root"},
            {"id": "later", "type": "session.thread_status_running", "session_thread_id": "child"}]
    threads = [{"id": "root", "status": "idle"}, {"id": "child", "status": "idle"}]
    assert child_threads(main, threads) == [threads[1]]


def test_configuration_readiness_discloses_custom_executor_gaps_across_members():
    graph = {"agents": {
        "root": {"tools": [{"type": "custom", "name": "render"}, {"type": "agent_toolset"}]},
        "child": {"tools": [{"type": "custom", "name": "render"}, {"type": "custom", "name": "review"}]},
    }}
    result = replay_capability({"replay_enabled": True}, graph, {"id": "replay", "config": {"type": "cloud"}})
    assert result == {"supported": True, "assessment": "configuration_only", "custom_tool_names": ["render", "review"]}
