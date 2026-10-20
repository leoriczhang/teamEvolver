# ruff: noqa: F401
"""Source enumeration uses only the unchanged public OV single-directory pagination API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import engine_resources as routes
from team_miner.skill_miner import ov_round_orchestration
from team_miner.skill_miner.ov_compile_mining import OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.miner.skill_miner.support.ov_round_bridge import FakeOV, Owner, create

ROOT = "viking://resources/source"

@pytest.fixture
def source_owner(tmp_path):
    return Owner(OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state")), FakeOV())

class OriginalDirectoryTransport:
    def __init__(self, paths, *, root=ROOT, failure=""):
        self.root, self.failure, self.calls = root, failure, []
        self.children = {root: {}}
        for relative in paths:
            parts, directory = relative.split("/"), root
            for index, name in enumerate(parts):
                uri = directory + "/" + name
                is_dir = index < len(parts) - 1
                self.children.setdefault(directory, {})[uri] = {
                    "uri": uri,
                    "name": name,
                    "isDir": is_dir,
                    "size": 0 if is_dir else 10,
                }
                if is_dir:
                    self.children.setdefault(uri, {})
                directory = uri

    def respond(self, request):
        assert request.method == "GET" and request.url.path == "/api/v1/fs/ls"
        params = request.url.params
        assert params["recursive"] == "false" and params["output"] == "original"
        assert params["sort_by"] == "name" and params["sort_order"] == "asc"
        assert params["show_all_hidden"] == "false" and "level_limit" not in params
        uri, offset, limit = params["uri"], int(params["offset"]), int(params["node_limit"])
        self.calls.append((uri, offset, limit))
        if self.failure == "missing" or uri not in self.children:
            return httpx.Response(404, json={"detail": "NOT_FOUND"})
        if self.failure == "child_missing" and uri != self.root:
            return httpx.Response(404, json={"detail": "NOT_FOUND"})
        rows = sorted(self.children[uri].values(), key=lambda row: (not row["isDir"], row["name"].lower(), row["name"]))
        if self.failure == "ignored_offset":
            offset = 0
        result = rows[offset : offset + limit]
        if self.failure == "foreign":
            result = [{"uri": "viking://resources/foreign.md", "isDir": False}]
        elif self.failure == "recursive":
            result = [{"uri": uri + "/nested/file.md", "isDir": False}]
        elif self.failure == "denied":
            result = [{"uri": uri + "/private", "isDir": True, "access": "denied"}]
        elif self.failure == "truncated":
            result = {"entries": result, "truncated": True}
        elif self.failure == "invalid":
            result = {"not_entries": "an unsupported shape"}
        return httpx.Response(200, json={"status": "ok", "result": result})

class EnumerationOwner(routes.MiningResourceMixin):
    def __init__(self, transport):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport.respond))

    async def _knowledge_request(self, account, method, path, **kwargs):
        response = await self.client.request(
            method, "http://inert-openviking.invalid" + path, params=kwargs.get("params"))
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return response.json()["result"]

    def _knowledge_endpoint(self):
        return "http://inert-openviking.invalid"

    def _knowledge_headers(self, account, **_kwargs):
        return {"X-OpenViking-Account": account}

    def _knowledge_http(self):
        return self.client

def tree_result(remote, **kwargs):
    async def run():
        owner = EnumerationOwner(remote)
        try:
            return await owner._knowledge_tree("default", remote.root, **kwargs)
        finally:
            await owner.client.aclose()

    return asyncio.run(run())
