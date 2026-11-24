import asyncio
import json
import time

import httpx
import pytest

from team_ma.discovery import replay_capability
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


def test_model_failure_reports_upstream_error_code_instead_of_missing_response():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events[:] = [e for e in events if e["type"] != "agent.message"]
                    events.insert(-1, {
                        "id": "model-error", "type": "session.error",
                        "processed_at": "2026-10-20T12:00:03Z",
                        "error": {"code": "model_request_failed_error", "message": json.dumps({
                            "error": {"code": "InvalidParameter.UnsupportedFileFormat",
                                      "message": "The file mimetype image/png is not supported."},
                        })},
                    })
                    events[-1]["stop_reason"] = {"type": "retries_exhausted"}
            return response

    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    try:
        with pytest.raises(RuntimeError, match="InvalidParameter.UnsupportedFileFormat"):
            session.send("use screenshot")
    finally:
        session.close()
    assert cleaned == ["run"]


def test_real_root_thread_id_is_main_response_and_projection_not_a_child():
    class Server(FakeMA):
        def respond(self, request):
            if request.url.path.endswith("/threads"):
                return httpx.Response(200, json={"data": [
                    {"id": "root-thread", "status": "idle"}, {"id": "child", "status": "idle"},
                ]})
            if "/threads/child/events" in request.url.path:
                count = sum(len(events) for events in self.events.values())
                return httpx.Response(200, json={"data": [{
                    "id": "child-reply-" + str(count), "type": "agent.message", "session_thread_id": "child",
                    "processed_at": "2026-10-20T12:00:02Z", "content": [{"type": "text", "text": "child output"}],
                }] if count > 1 else []})
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/sessions"):
                self.events[response.json()["id"]].append({
                    "id": "root-running", "type": "session.thread_status_running",
                    "session_thread_id": "root-thread", "processed_at": "2026-10-20T12:00:00Z",
                })
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    for event in events:
                        if event["type"].startswith(("agent.", "span.")):
                            event["session_thread_id"] = "root-thread"
            return response
    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    try:
        observed = session.send("write a note")
        assert "completed turn" in observed.response and "child output" not in observed.response
        main = [m for m in observed.messages if not m.get("session_thread_id") and m["role"] == "assistant"]
        assert any("completed turn" in m["content"] for m in main)
        assert any(m.get("session_thread_id") == "child" for m in observed.messages)
        assert session.send("continue").response
    finally:
        session.close()


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


def test_declared_custom_tools_preserve_snapshot_and_allow_builtin_execution():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events.insert(-1, {
                        "id": "builtin", "type": "agent.tool_use", "name": "Bash",
                        "input": {"command": "ffprobe -version"},
                        "processed_at": "2026-10-20T12:00:03Z",
                    })
            return response

    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    tools = [{"type": "builtin", "name": "Bash"},
             {"type": "custom", "name": "render_video", "description": "Render video",
              "input_schema": {"type": "object", "properties": {}}}]
    session.record["agent_graph"]["agents"]["root"]["tools"] = tools
    session.record["config"] = {"replay_enabled": True, "environment_id": "production"}
    session.record["replay_environment"]["config"]["type"] = "cloud"
    session.cap["replay_capability"] = replay_capability
    try:
        observed = session.send("check ffprobe with Bash")
        assert observed.response and observed.metrics["tool_call_count"] == 1
        assert observed.metrics["total_tokens"] == 12
        actual = next(iter(server.sessions.values()))
        assert actual["agent"]["type"] == "agent_with_overrides"
        assert actual["agent"]["tools"] == tools
    finally:
        session.close()
    assert cleaned == ["run"]


@pytest.mark.parametrize("thread_id", [None, "child"])
@pytest.mark.parametrize("requires_action", [False, True])
def test_custom_tool_request_fails_with_identity_and_archives_events(thread_id, requires_action):
    call = {"id": "custom-call", "type": "agent.custom_tool_use", "name": "render_video",
            "input": {"prompt": "private task content"}, "processed_at": "2026-10-20T12:00:03Z"}
    if thread_id:
        call["session_thread_id"] = thread_id

    class Server(FakeMA):
        def respond(self, request):
            if request.url.path.endswith("/threads"):
                return httpx.Response(200, json={"data": [{"id": "child", "status": "idle"}] if thread_id else []})
            if "/threads/child/events" in request.url.path:
                # Only the history read after root end_turn reveals the child call.
                history = [call]
                if requires_action:
                    history.append({"id": "child-idle", "type": "session.thread_status_idle",
                                    "session_thread_id": thread_id, "stop_reason": {"type": "requires_action"}})
                return httpx.Response(200, json={"data": history if any(self.events.values()) else []})
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    if not thread_id:
                        events.insert(-1, call)
                    if requires_action and not thread_id:
                        events[-1]["stop_reason"] = {"type": "requires_action"}
            return response

    server, cleaned, archived = Server(), [], {}
    session = session_for(server, cleaned)

    async def archive(name, data, owner):
        archived[name] = json.loads(data)
        return await save_artifact(name, data, owner)

    session.cap["save_artifact"] = archive
    error_pattern = "custom tool executor unavailable.*render_video.*custom-call"
    try:
        with pytest.raises(ReplayUnsupported, match=error_pattern) as exc:
            session.send("do the task")
        assert "private task content" not in str(exc.value)
        if thread_id:
            assert "child" in str(exc.value)
    finally:
        session.close()
    assert cleaned == ["run"]
    assert call in archived["ma-final-session.json"]["events"]
    assert session.diagnostics()["final_history"]["path"] == "ma-final-session.json"
    assert not any(b"tool_result" in r.content for r in server.requests if r.method == "POST")


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
    assert observed.execution_snapshot["runtime_ready_verified"] is True
    assert observed.execution_snapshot["runtime_ready_proof"]["path"] == "team-evolver-runtime-ready.json"
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
    assert saved[0][0] == "ma-session-snapshot.json"
    assert saved[1][0] == "ma-events-turn-1.json"
    assert saved[1][1] == server.events[session.session_id]
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


@pytest.mark.parametrize("registered", [True, False])
def test_write_output_must_be_downloaded_before_branch_completion_and_cleanup(registered, monkeypatch):
    class Server(FakeMA):
        polls = 0

        def respond(self, request):
            if request.url.host == "outputs.example":
                return httpx.Response(200, content=b'{"shots":[1,2,3]}')
            if request.url.path.endswith("/files") and self.events:
                self.polls += 1
                return httpx.Response(200, json={"data": [{
                    "id": "storyboard", "filename": "storyboard.json",
                    "download_url": "https://outputs.example/storyboard",
                }] if registered and self.polls >= 2 else []})
            result = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events.insert(-1, {
                        "id": "native-write", "type": "agent.tool_use", "name": "Write",
                        "input": {"file_path": "/mnt/session/outputs/storyboard.json"},
                        "processed_at": "2026-10-20T12:00:03Z",
                    })
            return result

    server, cleaned = Server(), []
    session = session_for(server, cleaned)
    monkeypatch.setattr("team_replay.adapters.managed_agents.ARTIFACT_REGISTRATION_SECONDS", .05)
    try:
        if registered:
            observed = session.send("create a storyboard")
            assert server.polls == 2 and not cleaned
            artifact = next(a for a in observed.artifacts if a["path"] == "storyboard.json")
            assert artifact["evidence"] == {"kind": "json", "available": True, "content": {"shots": [1, 2, 3]}}
        else:
            with pytest.raises(ReplayUnsupported, match="Declared MA outputs unavailable"):
                session.send("create a storyboard")
    finally:
        session.close()
    assert cleaned == ["run"]


@pytest.mark.parametrize("filename", [
    "storyboard.json",
    "family-insurance-ch2/reports/storyboard.json",
    "/mnt/session/outputs/family-insurance-ch2/reports/storyboard.json",
])
def test_nested_display_output_matches_downloaded_ma_filename(filename, monkeypatch):
    expected = "family-insurance-ch2/reports/storyboard.json"

    class Server(FakeMA):
        def respond(self, request):
            if request.url.host == "outputs.example":
                return httpx.Response(200, content=b'{"shots":[1,2,3]}')
            if request.url.path.endswith("/files") and self.events:
                return httpx.Response(200, json={"data": [{
                    "id": "storyboard", "filename": filename,
                    "download_url": "https://outputs.example/storyboard",
                }]})
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    next(e for e in events if e["type"] == "agent.message")["content"] = [{
                        "type": "text", "text": f"<display>/mnt/session/outputs/{expected}</display>",
                    }]
            return response

    session = session_for(Server(), [])
    monkeypatch.setattr("team_replay.adapters.managed_agents.ARTIFACT_REGISTRATION_SECONDS", .01)
    try:
        observation = session.send("create the report")
        artifact = next(a for a in observation.artifacts if a["path"] == expected)
        assert artifact["evidence"]["content"] == {"shots": [1, 2, 3]}
    finally:
        session.close()


@pytest.mark.parametrize("filenames,declared", [
    (["storyboard.json"], ["one/storyboard.json", "two/storyboard.json"]),
    (["storyboard.json", "storyboard.json"], ["one/storyboard.json"]),
    (["other/storyboard.json"], ["one/storyboard.json"]),
    (["one/storyboard.json", "storyboard.json"], ["one/storyboard.json", "two/storyboard.json"]),
])
def test_nested_output_does_not_accept_ambiguous_or_other_directory_files(filenames, declared, monkeypatch):
    class Server(FakeMA):
        def respond(self, request):
            if request.url.host == "outputs.example":
                return httpx.Response(200, content=b'{"shots":[1,2,3]}')
            if request.url.path.endswith("/files") and self.events:
                return httpx.Response(200, json={"data": [{
                    "id": str(i), "filename": name, "download_url": f"https://outputs.example/{i}",
                } for i, name in enumerate(filenames)]})
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    next(e for e in events if e["type"] == "agent.message")["content"] = [{
                        "type": "text",
                        "text": "\n".join(f"<display>/mnt/session/outputs/{p}</display>" for p in declared),
                    }]
            return response

    session = session_for(Server(), [])
    monkeypatch.setattr("team_replay.adapters.managed_agents.ARTIFACT_REGISTRATION_SECONDS", .01)
    try:
        with pytest.raises(ReplayUnsupported, match="Declared MA outputs unavailable"):
            session.send("create the reports")
    finally:
        session.close()


def test_root_thread_confirmation_is_actionable_without_waiting_for_deadline():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/sessions"):
                self.events[response.json()["id"]].append({
                    "id": "root-running", "type": "session.thread_status_running",
                    "session_thread_id": "root-thread", "processed_at": "2026-10-20T12:00:00Z",
                })
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events[-1].update(session_thread_id="root-thread", stop_reason={"type": "requires_action"})
            return response

    session = session_for(Server(), [])
    try:
        with pytest.raises(ReplayUnsupported, match="confirmation"):
            session.send("do the task")
    finally:
        session.close()


def test_upstream_session_error_without_idle_is_reported():
    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events[-1] = {"id": "fatal", "type": "session.error",
                                  "processed_at": "2026-10-20T12:00:09Z",
                                  "error": {"code": "environment_failed"}}
            return response

    session = session_for(Server(), [])
    try:
        with pytest.raises(RuntimeError, match="environment_failed"):
            session.send("do the task")
    finally:
        session.close()


def test_archive_write_failure_still_cleans_remote_resources():
    cleaned = []
    session = session_for(FakeMA(), cleaned)
    session.send("do the task")

    async def broken_archive(*args):
        raise OSError("object store unavailable")

    session.cap["save_artifact"] = broken_archive
    with pytest.raises(OSError, match="object store"):
        session.close()
    assert cleaned == ["run"]


def test_runtime_probe_failure_keeps_snapshot_and_reports_tool_service_error():
    from types import SimpleNamespace

    from team_replay.adapters.guard import GuardedSession
    from team_replay.execution import run_branch

    class Server(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "POST" and request.url.path.endswith("/events"):
                for events in self.events.values():
                    events.insert(-1, {
                        "id": "bootstrap-tool-error", "type": "agent.tool_result", "is_error": True,
                        "processed_at": "2026-10-20T12:00:03Z",
                        "content": [{"type": "text", "text": "task service unavailable: runtime issue"}],
                    })
            return response

    session = session_for(Server(), [])
    session.deadline = time.monotonic() + .2
    session.record["config"]["runtime_binding"] = {"ready_file": "state.json", "ready_probe_message": "read ready"}
    session.cap["runtime_binding"] = lambda env, binding, files: (env, "fixed-tree")
    session.record["config"]["runtime_binding"]["source"] = "openviking"
    result = run_branch(
        SimpleNamespace(open=lambda _: GuardedSession(session, lambda: None)), session.context,
        {"query": "do the task", "checks": [{"id": "output", "kind": "output", "text": "write the file"}]},
        harness={}, max_interactions=1,
    )
    assert result["completed"] is False
    assert "task_service_unavailable" in result["error"]
    assert result["execution_snapshot"]["session_id"] == session.session_id
    assert result["execution_snapshot"]["runtime_ready_verified"] is False
    assert result["runtime_diagnostics"]["observed_metrics"]["total_tokens"] == 12
    assert result["runtime_diagnostics"]["final_history"]["path"] == "ma-final-session.json"
