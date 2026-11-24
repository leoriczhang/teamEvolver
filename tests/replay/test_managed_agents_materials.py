"""Reuse original native inputs only after checking the frozen bytes."""

import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from team_replay.adapters.managed_agents import ManagedAgentsFactory
from team_replay.hooks import ReplayContext, ReplayTreatment
from teamEvolver.managed_agents import MAError
from tests.support.managed_agents import FakeEffects, FakeMA, save_artifact


@pytest.mark.parametrize("state", ["active", "quota", "mismatch", "missing", "expired", "unavailable", "unauthorized"])
@pytest.mark.parametrize("input_type", [None, "video"])
def test_reuse_verified_original_inputs_without_owning_or_deleting_them(state, input_type):
    raw = b"original pinned input"
    effects = FakeEffects()

    class Server(FakeMA):
        def __init__(self):
            super().__init__()
            self.uploads = []

        def respond(self, request):
            if state == "quota":
                if request.method == "POST" and request.url.path.endswith("/sessions"):
                    if json.loads(request.content).get("resources"):
                        return httpx.Response(403, json={"error": {"code": "OperationDenied.FileQuotaExceeded"}})
                if request.url.path.endswith("/vaults/isolated/credentials"):
                    return httpx.Response(200, json={"data": [
                        {"auth": {"type": "environment_variable", "secret_name": "ARK_API_KEY"}}]})
            if request.url.host == "download.example":
                assert "authorization" not in request.headers
                return httpx.Response(200, content=b"changed" if state == "mismatch" else raw)
            if request.url.path.endswith("/files/original"):
                if state in {"missing", "unauthorized"}:
                    return httpx.Response(404 if state == "missing" else 401,
                                          json={"error": {"code": "FileUnavailable"}})
                return httpx.Response(200, json={
                    "id": "original", "status": "expired" if state == "expired" else "active",
                    "download_url": "https://download.example/input?signature=private",
                })
            if request.method == "POST" and request.url.path.endswith("/files"):
                assert state not in {"active", "quota"}, "Verified source inputs must not consume Files quota"
                self.uploads.append(request.content)
                return httpx.Response(200, json={"id": "uploaded-" + str(len(self.uploads))})
            return super().respond(request)

    server = Server()
    graph = {"entry_agent_id": "root", "agents": {"root": {"id": "root", "version": 1, "skills": []}}}
    record = {"connection_id": "one", "config": {"replay_vault_ids": ["isolated"]},
              "agent_graph": graph, "skill_bindings": {},
              "environment": {"config": {"type": "cloud"}}, "replay_environment": {"id": "isolated", "config": {}}}

    async def client(record):
        return server.client()

    async def original_file_id(record, material):
        assert record["connection_id"] == "one" and material["source_session_id"] == "source"
        return "" if state == "unavailable" else "original"

    async def cleanup(*args):
        # Only owned effects are eligible for cleanup.
        for effect in effects.records:
            if effect["kind"] == "replay-file":
                assert effect["receipt"]["id"] != "original"

    cap = {"run": lambda coro, timeout=None: asyncio.run(coro), "client": client,
           "original_file_id": original_file_id, "effects": lambda *args: effects, "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    sessions = []
    try:
        for branch in ("baseline", "candidate"):
            context = ReplayContext(
                request_id=branch, runtime_type="managed-agents", treatment=ReplayTreatment(branch, None),
                materials=({"path": "uploads/input.bin", "content_b64": base64.b64encode(raw).decode(),
                            "sha256": hashlib.sha256(raw).hexdigest(), "source_session_id": "source",
                            **({"input_type": input_type, "media_type": "video/mp4", "fps": 2}
                               if input_type else {})},),
                context_snapshot={}, timeout_seconds=30,
                source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
            )
            session = ManagedAgentsFactory({"case": record}, cap).open(context)
            sessions.append(session)
            if state == "unauthorized":
                with pytest.raises(MAError, match="401"):
                    session.send("Read the input")
                assert not server.sessions and not server.uploads
                return
            session.send("Read the input")
            if input_type:
                assert session.material_blocks == [
                    {"type": "video", "source": {"type": "file", "file_id":
                     "original" if state in {"active", "quota"} else "uploaded-" + str(len(server.uploads)),
                     "fps": 2}},
                ]
                submitted = next(e for e in server.events[session.session_id] if e["type"] == "user.message")
                assert session.material_blocks[0] in submitted["content"]
            proof = session.execution_snapshot["materials"][0]
            expected = {"active": "verified_original", "quota": "sandbox_download"}.get(state, "uploaded")
            assert proof["transport"] == expected
            assert proof["sha256"] == hashlib.sha256(raw).hexdigest()
            assert "signature" not in json.dumps(session.execution_snapshot)
        assert len(server.sessions) == 2
        if state == "quota":
            assert not server.uploads
            assert all(not value["resources"] for value in server.sessions.values())
            scripts = [value["environment"]["config"]["setup_script"] for value in server.sessions.values()]
            assert len(set(scripts)) == 1
            assert hashlib.sha256(raw).hexdigest() in scripts[0]
            assert "private" not in scripts[0]
            if not input_type:
                assert "/tmp/te-replay-inputs/input.bin" in str(session.material_blocks)
            assert proof["sandbox_path"] == "/tmp/te-replay-inputs/input.bin"
            assert all(e["kind"] != "replay-file" for e in effects.records)
            return
        mounts = [value["resources"][0] for value in server.sessions.values()]
        assert all(m["mount_path"] == "input.bin" for m in mounts)
        if state == "active":
            assert not server.uploads
            assert all(m["file_id"] == "original" for m in mounts)
            assert all(e["kind"] != "replay-file" for e in effects.records)
        else:
            assert len(server.uploads) == 2 and all(raw in data for data in server.uploads)
            assert len({m["file_id"] for m in mounts}) == 2
    finally:
        for session in sessions:
            session.close()
