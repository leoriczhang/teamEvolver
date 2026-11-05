"""Stateful MA HTTP fake shared by client, collector and Replay contract tests."""

import copy
import io
import json
import zipfile

import httpx

from teamEvolver.managed_agents import MAClient


class _Stream(httpx.AsyncByteStream):
    def __init__(self, server, session):
        self.server, self.session = server, session

    async def __aiter__(self):
        yield b": ready\n\n"
        for event in self.server.events[self.session][-4:]:
            yield ("data: " + json.dumps(event) + "\n\n").encode()


class FakeMA:
    def __init__(self):
        self.requests, self.events, self.sessions, self.agents, self.skills = [], {}, {}, {}, {}
        self.count = 0
        self.deleted = []

    def client(self, base_url="https://ma.example/api/v3", token="test"):
        return MAClient(base_url, token, transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v3/")
        parts = path.split("/")
        if request.method == "POST":
            self.count += 1
            if path == "skills":
                name = "skill-" + str(self.count)
                self.skills[name] = request.content
                return httpx.Response(200, json={"id": name, "latest_version": "1"})
            payload = json.loads(request.content or b"{}")
            if path == "agents":
                ident = "agent-" + str(self.count)
                self.agents[ident] = {"id": ident, "version": 1, **payload}
                return httpx.Response(200, json=self.agents[ident])
            if path == "sessions":
                assert all(isinstance(tag, dict) and set(tag) == {"key", "value"} for tag in payload.get("tags", []))
                ident = "session-" + str(self.count)
                self.sessions[ident] = {"id": ident, "status": "idle", **payload}
                self.events[ident] = []
                return httpx.Response(200, json=self.sessions[ident])
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "events":
                ident = parts[1]
                index = len(self.events[ident]) + 1
                user = {"id": f"{ident}:u:{index}", "processed_at": f"2026-10-20T12:00:{index:02}Z",
                        **payload["events"][0]}
                usage = {"id": f"{ident}:m:{index}", "type": "span.model_request_end",
                         "processed_at": f"2026-10-20T12:00:{index + 1:02}Z", "model_usage": {
                             "input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0,
                         }}
                reply = {"id": f"{ident}:a:{index}", "type": "agent.message",
                         "processed_at": f"2026-10-20T12:00:{index + 2:02}Z",
                         "content": [{"type": "text", "text": "completed turn " + str(index)}]}
                end = {"id": f"{ident}:e:{index}", "type": "session.status_idle",
                       "processed_at": f"2026-10-20T12:00:{index + 3:02}Z", "stop_reason": {"type": "end_turn"}}
                self.events[ident].extend([user, usage, reply, end])
                return httpx.Response(200, json={"data": [user]})
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(200, json={})
        if parts[0] == "sessions":
            ident = parts[1]
            if len(parts) == 4 and parts[-1] == "stream":
                return httpx.Response(200, stream=_Stream(self, ident), headers={"content-type": "text/event-stream"})
            if len(parts) == 3 and parts[-1] == "events":
                return httpx.Response(200, json={"data": self.events[ident]})
            if len(parts) == 3 and parts[-1] == "threads":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json=self.sessions[ident])
        if path == "files":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if parts[0] == "agents":
            return httpx.Response(200, json=self.agents[parts[1]])
        raise AssertionError(f"Unexpected MA request {request.method} {path}")


class FakeEffects:
    def __init__(self):
        self.records = []

    async def apply(self, effect_id, kind, intent, send, reconcile=None):
        receipt = await send()
        self.records.append({"effect_id": effect_id, "kind": kind, "intent": copy.deepcopy(intent), "receipt": receipt})
        return receipt


async def save_artifact(name, data, owner):
    return {"path": name, "size": len(data), "object_ref": f"fake/{owner}/{name}"}


def skill_zip(name="note-craft", body="Write useful notes."):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(name + "/SKILL.md", f"---\nname: {name}\ndescription: Useful notes\n---\n{body}\n")
    return data.getvalue()
