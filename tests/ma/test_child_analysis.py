"""Child Skill evidence must survive the MA projection into model analysis."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from team_ma import collection
from team_skills.evolution.session_processing import groups
from team_skills.evolution.stages.analyze import build_analysis_payload
from team_skills.evolution.stages.summarize import _extract_session_metadata, build_session_trajectory
from team_skills.evolution.stages.trace_analyze import analyze_session_traces, build_trace_payload
from teamEvolver.managed_agents.events import turns


def child_session():
    events = [
        {"id": "u", "type": "user.message", "content": [{"type": "text", "text": "Produce a video"}]},
        {"id": "delegate", "type": "agent.thread_message_sent", "to_session_thread_id": "producer",
         "to_agent_name": "Producer", "content": [{"type": "text", "text": "Render and validate"}]},
        {"id": "read", "type": "agent.tool_use", "session_thread_id": "producer", "agent_name": "Producer",
         "name": "bash", "input": {"command": "cat /mnt/skills/render-quality/SKILL.md"}},
        {"id": "tool", "type": "agent.tool_result", "session_thread_id": "producer", "tool_use_id": "read",
         "content": [{"type": "text", "text": "Check geometry before rendering"}]},
        {"id": "child", "type": "agent.message", "session_thread_id": "producer", "agent_name": "Producer",
         "content": [{"type": "text", "text": "Render failed because the mesh was invalid"}]},
        {"id": "a", "type": "agent.message", "content": [{"type": "text", "text": "Task finished"}]},
    ]
    return {"session_id": "ma-parent", "turns": turns(events, {"skill-quality": "render-quality"})}


def test_child_tools_results_and_response_reach_session_and_trace_models():
    session = child_session()
    _extract_session_metadata(session)
    session["_trajectory"] = build_session_trajectory(session)
    payload = build_analysis_payload(session)
    trace = build_trace_payload(session)["traces"][0]
    interaction = payload["interactions"][0]
    assert interaction.get("tool_calls"), "MA tools are missing from the Session Analyze input"
    assert interaction.get("tool_results"), "MA tool results are missing from the Session Analyze input"
    assert "Render failed" in str(interaction.get("agent_threads"))
    assert "producer" in str(interaction["agent_threads"])
    assert trace.get("tool_calls"), "Trace Analyze must see actual tool evidence"
    assert "Render failed" in str(trace.get("agent_threads"))
    assert payload["used_skills"] == ["render-quality"]
    assert groups([session]) == {"render-quality": [session]}


def test_codingvideo_workspace_skill_paths_are_actual_usage_evidence():
    session = {"session_id": "ma", "turns": turns([
        {"id": "u", "type": "user.message", "content": [{"type": "text", "text": "Render"}]},
        {"id": "tool", "type": "agent.tool_use", "session_thread_id": "producer", "name": "bash",
         "input": {"command": "cat /workspace/skills/render-quality/SKILL.md"}},
        {"id": "a", "type": "agent.message", "content": [{"type": "text", "text": "Done"}]},
    ], {"skill-quality": "render-quality", "skill-unused": "unused"})}
    _extract_session_metadata(session)
    assert set(groups([session])) == {"render-quality"}


def test_child_gets_its_own_trace_verdict_with_parent_and_skill_attribution():
    class Client:
        async def chat(self, messages, **kwargs):
            traces = json.loads(messages[-1]["content"])["traces"]
            return json.dumps({"traces": [
                {"trace_id": t["trace_id"], "turn_num": t["turn_num"],
                 "is_badcase": bool(t.get("session_thread_id")), "problem_type":
                 "wrong_output" if t.get("session_thread_id") else "other", "problem_description":
                 "子线程模型破面导致渲染失败" if t.get("session_thread_id") else ""}
                for t in traces
            ]})

    session = child_session()
    result = asyncio.run(analyze_session_traces(Client(), session))
    assert len(result) == 2
    parent, child = result
    assert parent["is_badcase"] is False and child["is_badcase"] is True
    assert child["parent_trace_id"] == parent["trace_id"]
    assert child["session_id"] == parent["session_id"] == "ma-parent"
    assert child["session_thread_id"] == "producer"
    assert child["used_skills"] == ["render-quality"]


def test_late_child_events_are_collected_and_old_projection_is_reanalyzed(monkeypatch):
    accepted, state = [], {}
    events = [
        {"id": "u", "type": "user.message", "processed_at": "2026-10-23T10:00:00Z",
         "content": [{"type": "text", "text": "Render"}]},
        {"id": "a", "type": "agent.message", "processed_at": "2026-10-23T10:00:01Z",
         "content": [{"type": "text", "text": "Done"}]},
        {"id": "e", "type": "session.status_idle", "processed_at": "2026-10-23T10:00:02Z",
         "stop_reason": {"type": "end_turn"}},
    ]
    child = [
        {"id": "read", "type": "agent.tool_use", "processed_at": "2026-10-23T10:00:03Z",
         "name": "bash", "input": {"command": "cat /workspace/skills/render-quality/SKILL.md"}},
        {"id": "reply", "type": "agent.message", "processed_at": "2026-10-23T10:00:04Z",
         "content": [{"type": "text", "text": "Render failed: mesh invalid"}]},
    ]

    class Client:
        async def request(self, method, path):
            return {"id": "parent", "status": "idle", "tags": []}

        async def history(self, session, thread=""):
            return child if thread else events

        async def listing(self, path, **kwargs):
            return [{"id": "producer", "status": "idle"}]

    class Repo:
        def __init__(self, tx):
            pass

        async def cursor(self, *args):
            return {"record": state} if state else None

        async def save_cursor(self, conn, **kwargs):
            state.clear()
            state.update(kwargs["record"])

        async def all_resources(self, *args, **kwargs):
            return [{"logical_name": "render-quality", "record": {"id": "skill-quality"}}]

    @asynccontextmanager
    async def tenant_tx(tenant):
        yield None

    async def noop(*args, **kwargs):
        pass

    async def runtime(*args):
        return {"agent_graph": {"agents": {}}, "environment": {"config": {}}, "snapshot_missing": False}

    async def save(*args):
        return {"object_ref": "fake/events"}

    async def accept(**kwargs):
        accepted.append(kwargs)
        return SimpleNamespace(job_id="job", dedupe=False)

    monkeypatch.setattr(collection, "MARepository", Repo)
    monkeypatch.setattr(collection, "runtime_snapshot", runtime)
    monkeypatch.setattr(collection, "save_object", save)
    services = SimpleNamespace(tx=SimpleNamespace(tenant_tx=tenant_tx),
                               tasks=SimpleNamespace(require_current=noop),
                               extras={"ma_accept_session": accept})
    task = SimpleNamespace(tenant_id="tenant", subject_id="connection")
    connection = {"revision": 1, "config": {"user_tag_prefix": "te-user:", "default_user_id": "one"}}

    async def run():
        assert await collection.collect_one(task, services, Client(), connection, "parent", set()) == "accepted"
        projected = accepted[0]["session"]["turns"][0]
        assert projected["used_skills"] == ["render-quality"]
        assert "mesh invalid" in str(projected["agent_threads"])
        assert await collection.collect_one(task, services, Client(), connection, "parent", set()) == "unchanged"
        state.pop("projection_revision")
        assert await collection.collect_one(task, services, Client(), connection, "parent", set()) == "accepted"
        assert accepted[0]["session"]["session_id"] == accepted[1]["session"]["session_id"]
        assert accepted[0]["dedupe_key"] != accepted[1]["dedupe_key"]

    asyncio.run(run())


def test_runtime_bundle_paths_and_unread_catalog_have_distinct_attribution():
    result = turns([
        {"id": "u", "type": "user.message", "content": [{"type": "text", "text": "Make a video"}]},
        {"id": "catalog", "type": "agent.message", "content": [
            {"type": "text", "text": "Available skills: render-quality and production-core"}]},
        {"id": "t", "type": "agent.tool_use", "session_thread_id": "producer",
         "name": "bash", "input": {"command": "node /workspace/codingvideo/runtime/run.mjs"}},
    ], {"remote-core": "production-core", "remote-quality": "render-quality"},
        {"/workspace/codingvideo/runtime/": "production-core"})
    assert result[0]["used_skills"] == ["production-core"]
    assert result[0]["agent_threads"][0]["skill_refs"][0]["session_thread_id"] == "producer"


def test_native_ma_skill_tool_selection_enters_the_child_skill_group():
    session = {"session_id": "parent", "turns": turns([
        {"id": "u", "type": "user.message", "content": [{"type": "text", "text": "Produce a video"}]},
        {"id": "select", "type": "agent.tool_use", "session_thread_id": "producer",
         "name": "skill", "input": {"name": "codingvideo-audio"}},
        {"id": "catalog", "type": "agent.tool_use", "name": "grep",
         "input": {"pattern": "codingvideo-delivery"}},
    ], {"remote-audio": "codingvideo-audio", "remote-delivery": "codingvideo-delivery"})}
    _extract_session_metadata(session)
    assert set(groups([session])) == {"codingvideo-audio"}
    child = session["turns"][0]["agent_threads"][0]
    assert child["used_skills"] == ["codingvideo-audio"]
    assert child["skill_refs"][0]["source"] == "skill-tool"
