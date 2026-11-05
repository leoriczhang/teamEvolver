import asyncio
import io
import zipfile

import httpx
import pytest

from team_ma.discovery import agent_graph, read_skill_zip
from teamEvolver.managed_agents import MAClient, MAError
from teamEvolver.managed_agents.events import complete, metrics, parse_sse, stable_end, tag_values, turns


def test_real_ma_key_value_tags_preserve_attribution_and_replay_ownership():
    assert tag_values([
        {"key": "te-user", "value": "employee42"}, {"key": "te-replay", "value": "run/branch"},
        {"key": "unrelated", "value": "x"}, "legacy:value",
    ]) == ["te-user:employee42", "te-replay:run/branch", "unrelated:x", "legacy:value"]


def test_real_semantic_skill_mount_path_is_usage_evidence():
    events = [{"id": "user", "type": "user.message", "content": [{"type": "text", "text": "make a note"}]},
              {"id": "read", "type": "agent.tool_use", "name": "read",
               "input": {"file_path": "/mnt/skills/note-craft/references/steps.md"}}]
    observed = turns(events, {"skill-remote-id": "note-craft"})[0]
    assert observed["used_skills"] == ["note-craft"]
    assert observed["skill_refs"][0]["event_id"] == "read"


def test_native_delegation_evidence_is_preserved_without_inventing_tool_calls():
    events = [
        {"id": "user", "type": "user.message", "content": [{"type": "text", "text": "make a note"}]},
        {"id": "child", "type": "session.thread_created", "session_thread_id": "thread", "agent_name": "writer"},
        {"id": "send", "type": "agent.thread_message_sent", "to_session_thread_id": "thread",
         "to_agent_name": "writer", "content": [{"type": "text", "text": "format acknowledgement"}]},
        {"id": "received", "type": "agent.thread_message_received", "from_session_thread_id": "thread",
         "from_agent_name": "writer", "content": [{"type": "text", "text": "done"}]},
        {"id": "reply", "type": "agent.message", "content": [{"type": "text", "text": "done"}]},
    ]
    turn = turns(events)[0]
    native = [m["ma_event"] for m in turn["messages"] if "ma_event" in m]
    assert [n["type"] for n in native] == [
        "session.thread_created", "agent.thread_message_sent", "agent.thread_message_received",
    ]
    assert native[1]["to_agent_name"] == native[2]["from_agent_name"] == "writer"
    assert turn["response_text"] == "done"
    assert metrics(events)["tool_call_count"] == 0
    assert not any("tool_calls" in message for message in turn["messages"])


def test_real_files_null_response_is_an_empty_page_until_outputs_exist():
    async def run():
        async with MAClient("https://ma.example", "k", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"null"),
        )) as client:
            assert await client.request("GET", "files") == {}
            assert await client.listing("files") == []
    asyncio.run(run())


def test_client_preserves_api_prefix_and_signed_download_never_receives_bearer():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "ma.example":
            if request.url.path.endswith("/content"):
                return httpx.Response(302, headers={"Location": "https://files.example/bundle?signature=hidden"})
            return httpx.Response(200, json={"id": "a", "version": 2})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"actual bytes")

    async def run():
        async with MAClient("https://ma.example/api/v3", "private", transport=httpx.MockTransport(respond)) as client:
            await client.request("GET", "/agents/a", params={"version": 2})
            assert await client.download("skills/s/versions/1/content") == b"actual bytes"
    asyncio.run(run())
    assert requests[0].url.path == "/api/v3/agents/a"
    assert requests[0].url.params["version"] == "2"
    assert requests[0].headers["authorization"] == "Bearer private"


def test_download_limit_error_redaction_and_unknown_post():
    def respond(request):
        if request.url.path.endswith("/large"):
            return httpx.Response(200, content=b"x" * 10)
        return httpx.Response(403, json={"error": {"code": "Forbidden", "message": "SECRET signature"}})

    async def run():
        async with MAClient("https://ma.example/api/v3", "SECRET", transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(MAError, match="DownloadTooLarge"):
                await client.download("large", max_bytes=5)
            with pytest.raises(MAError) as exc:
                await client.request("GET", "agents")
            assert "SECRET" not in str(exc.value)
        def timeout(request):
            raise httpx.ReadTimeout("SECRET", request=request)
        async with MAClient("https://ma.example", "SECRET", transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(MAError, match="TransportOutcomeUnknown"):
                await client.request("POST", "skills")
    asyncio.run(run())


def test_ready_preview_deduplication_and_measured_cache_usage():
    async def lines():
        for line in [": ready", "event: agent.message", 'data: {"id":"e","content":[]}', "", ""]:
            yield line
    async def run():
        return [e async for e in parse_sse(lines())]
    assert asyncio.run(run())[0] == {"type": "bridge.ready"}
    call = {"id": "m", "type": "span.model_request_end", "model_usage": {
        "input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 3,
    }}
    result = metrics([call, call, {"id": "t", "type": "agent.tool_use"}])
    assert result["total_tokens"] == 20
    assert result["tool_call_count"] == 1
    assert result["api_calls"] == 1
    assert "total_tokens" not in metrics([{"id": "m", "type": "span.model_request_end"}])
    assert complete([{"id": "p", "type": "event_delta"}, call, call]) == [call]
    assert stable_end([{"id": "e", "type": "session.status_idle", "stop_reason": {"type": "requires_action"}}]) is None


def test_generic_graph_preserves_self_and_pins_historical_members():
    requests = []
    def respond(request):
        requests.append(request)
        ident = request.url.path.split("/")[-1]
        value = {"id": ident, "version": 3 if ident == "root" else 2, "name": ident, "skills": []}
        if ident == "root":
            value["multiagent"] = {"type": "coordinator", "agents": [
                {"type": "self"}, {"type": "agent", "id": "writer", "version": 2},
            ]}
        return httpx.Response(200, json=value)
    async def run():
        async with MAClient("https://ma.example/api/v3", "k", transport=httpx.MockTransport(respond)) as client:
            return await agent_graph(client, "root", 3)
    graph = asyncio.run(run())
    assert set(graph["agents"]) == {"root", "writer"}
    assert requests[1].url.params["version"] == "2"
    assert graph["agents"]["root"]["multiagent"]["agents"][0]["type"] == "self"


@pytest.mark.parametrize("path", ["../SKILL.md", "/absolute/SKILL.md", "a/../../SKILL.md"])
def test_zip_traversal_is_rejected(path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(path, "---\nname: example\ndescription: sample\n---\n")
    with pytest.raises(ValueError):
        read_skill_zip(data.getvalue())
