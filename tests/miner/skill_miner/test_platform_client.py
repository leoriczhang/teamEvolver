"""Compiler packages use platform transport and preserve attachment integrity."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from team_miner.skill_miner.platform_client import CompileClientCompat, _skill_package_files, _skill_package_matches
from teamEvolver.openviking.client import OpenVikingClient

BODY = "---\nname: compiler\ndescription: Test compiler.\n---\n\nCompile the supplied Evidence.\n"


@pytest.mark.asyncio
async def test_package_install_reuses_platform_auth_and_multipart_then_verified_shared_root():
    calls = []
    guarded = []

    async def guard():
        guarded.append(True)

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/v1/resources/temp_upload":
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert b'filename="compiler.zip"' in request.content
            assert b"application/zip" in request.content
            return httpx.Response(200, json={"status": "ok", "result": {"temp_file_id": "temp-1"}})
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["temp_file_id"] == "temp-1" and "data" not in payload
            assert payload["source_metadata"]["source"] == "temp_upload"
            return httpx.Response(200, json={"status": "ok", "result": {"name": "compiler"}})
        return httpx.Response(200, json={"root_uri": "viking://agent/skills/compiler", "content": BODY})

    client = OpenVikingClient(endpoint="https://ov.test", api_key="test-key", account_id="tenant-account", user="team")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    compiler = CompileClientCompat(client=client, guard=guard)
    try:
        result = await compiler.install_skill(skill_name="compiler", skill_body=BODY,
                                               auxiliary_files={"scripts/compile.py": "print('frozen')\n"})
        assert result["ok"]
        assert (await compiler.get_skill(skill_name="compiler"))["ok"]
        assert len(guarded) >= 3
        for call in calls:
            assert call.headers["x-openviking-account"] == "tenant-account"
            assert call.headers["authorization"] == "Bearer test-key"
            assert call.headers["x-openviking-role"] == "admin"
    finally:
        await client.aclose()


def test_package_integrity_rejects_missing_or_changed_resources_and_unsafe_paths():
    files = _skill_package_files("compiler", BODY, {"references/source.md": "Evidence"})
    detail = {"content": BODY, "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                                         for path, data in files.items()]}
    assert _skill_package_matches(detail, BODY, files)
    assert not _skill_package_matches({**detail, "files": detail["files"][:1]}, BODY, files)
    changed = {**detail, "files": [{"path": "SKILL.md", "sha256": "0" * 64}, *detail["files"][1:]]}
    assert not _skill_package_matches(changed, BODY, files)
    with pytest.raises(ValueError):
        _skill_package_files("compiler", BODY, {"../outside.py": "bad"})


@pytest.mark.asyncio
async def test_lost_worker_fence_blocks_compiler_upload_before_transport():
    calls = []

    async def guard():
        raise RuntimeError("stale Worker")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = OpenVikingClient(endpoint="https://ov.test", api_key="test-key", account_id="tenant-account")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="stale"):
            await CompileClientCompat(client=client, guard=guard).install_skill(skill_name="compiler", skill_body=BODY)
        assert calls == []
    finally:
        await client.aclose()
