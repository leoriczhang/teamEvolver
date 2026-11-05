import asyncio
import json
import time

import httpx
import pytest

from team_replay.adapters.managed_agents import ManagedAgentsFactory
from team_replay.hooks import ReplayContext, ReplayTreatment, ReplayUnsupported
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from tests.support.managed_agents import FakeEffects, FakeMA, save_artifact


def session_for(server, cleaned):
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 1, "tools": [], "skills": [],
                 "multiagent": {"type": "coordinator", "agents": [{"type": "self"}]}}}}
    record = {"connection_id": "one", "config": {}, "agent_graph": graph, "skill_bindings": {},
              "environment": {"config": {"type": "cloud"}}, "replay_environment": {"id": "env", "config": {}}}
    async def client(record):
        return server.client()
    async def cleanup(connection, owner, client):
        cleaned.append(owner)
    cap = {"client": client, "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "run": lambda coroutine, timeout=None: asyncio.run(coroutine),
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    context = ReplayContext(request_id="run", runtime_type="managed-agents",
                            treatment=ReplayTreatment("baseline", None), materials=(), context_snapshot={},
                            timeout_seconds=10, source_session={"runtime": {"connection_id": "one",
                                                                           "agent_graph": graph}})
    return ManagedAgentsFactory({"case": record}, cap).open(context)


def test_missing_usage_is_unavailable_and_self_needs_no_clone():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events[:] = [e for e in events if e.get("type") != "span.model_request_end"]
            return response
    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    observation = session.send("write a note")
    assert "total_tokens" not in observation.metrics
    assert observation.metrics_incomplete_reason
    assert server.agents == {} and len(server.sessions) == 1
    session.close()
    assert cleaned == ["run"]


def test_tool_confirmation_is_not_fabricated_and_failure_cleans():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events[-1]["stop_reason"] = {"type": "requires_action", "event_ids": ["approval"]}
            return response
    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    try:
        with pytest.raises(ReplayUnsupported, match="confirmation"):
            session.send("write a note")
    finally:
        session.close()
    assert cleaned == ["run"]
    assert not any(b"tool_result" in request.content for request in server.requests if request.method == "POST")


def test_sse_disconnect_recovers_history_without_resending():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": ready\n\n"
            raise httpx.ReadError("stream disconnected")
    class Server(FakeMA):
        def respond(self, request):
            if request.url.path.endswith("/stream"):
                return httpx.Response(200, stream=Stream())
            return super().respond(request)
    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    assert session.send("write a note").response
    session.close()
    writes = [r for r in server.requests if r.method == "POST" and r.url.path.endswith("/events")]
    assert len(writes) == 1


def test_runtime_probe_isolated_from_business_response_and_included_in_real_usage():
    server, cleaned = FakeMA(), []
    session = session_for(server, cleaned)
    session.record["config"]["runtime_binding"] = {"ready_probe_message": "read runtime ready file"}
    session.runtime_tree = "actual-tree"
    proof = json.dumps({"ready": True, "runtime_tree_sha256": "actual-tree"}).encode()
    original = server.respond

    def respond(request):
        if request.url.host == "outputs.example":
            return httpx.Response(200, content=proof)
        if request.url.path.endswith("/files") and server.events:
            return httpx.Response(200, json={"data": [{
                "id": "proof", "filename": "team-evolver-runtime-ready.json",
                "download_url": "https://outputs.example/proof",
            }]})
        return original(request)

    server.respond = respond
    saved = []

    async def artifact(name, data, owner):
        saved.append((name, data))
        return {"path": name, "size": len(data)}

    session.cap["save_artifact"] = artifact

    async def start():
        session.client = server.client()
        session.effects = FakeEffects()
        value = await session.client.request("POST", "sessions", json={"agent": {"id": "root"}})
        session.session_id = value["id"]
        await session._ready()

    asyncio.run(start())
    assert session.turn_num == 0
    observed = session.send("perform actual business task")
    assert observed.metrics["api_calls"] == 2 and observed.metrics["total_tokens"] == 24
    assert "read runtime ready file" not in observed.response
    assert session.turn_num == 1 and saved
    session.close()


def test_binding_change_invalidates_old_target_acceptance():
    policy = {"required_runtimes": ["managed-agents"], "required_targets": [{
        "connection_id": "one", "connection_revision": 1, "binding_hash": "new", "config_hash": "config",
    }]}
    result = {"runtime_type": "managed-agents", "status": "evaluated", "accepted": True, "verdict": "accept",
              "target_validation": {"one": {"status": "evaluated", "accepted": True, "verdict": "accept",
                                            "connection_revision": 1, "binding_hash": "old", "config_hash": "config"}}}
    gate = evaluate_runtime_compatibility(policy, [result])
    assert gate["status"] == "blocked" and gate["target_matrix"]["one"]["status"] == "missing"


def test_child_finishes_after_root_idle_without_another_main_sse_event():
    class Stream(httpx.AsyncByteStream):
        def __init__(self, server, session):
            self.server, self.session = server, session
        async def __aiter__(self):
            yield b": ready\n\n"
            for event in self.server.events[self.session][-4:]:
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            await asyncio.sleep(0.1)
            self.server.child_done = True
            await asyncio.Event().wait()  # no more main events
    class Server(FakeMA):
        child_done = False
        def respond(self, request):
            if request.url.path.endswith("/stream"):
                return httpx.Response(200, stream=Stream(self, request.url.path.split("/")[-3]))
            if request.url.path.endswith("/threads"):
                return httpx.Response(200, json={"data": [{"id": "child", "status":
                                                          "idle" if self.child_done else "running"}]})
            if "/threads/child/events" in request.url.path:
                return httpx.Response(200, json={"data": []})
            return super().respond(request)
    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    assert session.send("do the task").response
    assert server.child_done
    session.close()


def test_exact_remote_events_are_archived_before_session_cleanup():
    server, cleaned, saved = FakeMA(), [], []
    session = session_for(server, cleaned)

    async def archive(name, data, owner):
        assert not cleaned
        saved.append((name, json.loads(data), owner))
        return {"path": name, "size": len(data)}

    session.cap["save_artifact"] = archive
    observed = session.send("do the task")
    assert saved[0][0] == "ma-events-turn-1.json"
    assert saved[0][1] == server.events[session.session_id]
    assert saved[0][2] == "run" and observed.metrics["total_tokens"] == 12
    session.close()
    assert saved[-1][0] == "ma-final-session.json"
    assert saved[-1][1]["events"] == server.events[session.session_id]
    assert saved[-1][1]["metrics"]["total_tokens"] == 12
    assert cleaned == ["run"]


def test_branch_deadline_has_actionable_error_message():
    server, cleaned = FakeMA(), []
    session = session_for(server, cleaned)
    session.deadline = time.monotonic() + .05

    async def never_finishes(message):
        await asyncio.Event().wait()

    session._send = never_finishes
    with pytest.raises(TimeoutError, match="MA Replay branch deadline exceeded"):
        session.send("do the task")
    session.close()
