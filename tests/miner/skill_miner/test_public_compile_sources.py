"""Public gateway source admission is stricter than the internal file Map API."""

from __future__ import annotations

import asyncio

import pytest
from starlette.exceptions import HTTPException

from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner, start


def test_public_gateway_rejects_file_source_before_idempotent_acceptance():
    async def check():
        transport = FakeCompile()
        uri = "viking://resources/private-input/packet-00.json"
        transport.contents[uri] = "{}"
        transport.directories.add(uri.rsplit("/", 1)[0])
        with pytest.raises(HTTPException, match="Compile source must be a directory"):
            await transport.request(
                "test",
                "POST",
                "/api/v1/compile",
                json={
                    "from": [uri],
                    "to": "viking://resources/private-output",
                    "skill": "viking://agent/skills/test-compiler",
                    "instruction": "Process each file separately.",
                },
                extra_headers={"Idempotency-Key": "file-source-must-not-be-accepted"},
            )
        assert not transport.accepted
        assert not transport.tasks
        assert not transport.calls

    asyncio.run(check())


def test_production_staged_request_passes_public_directory_admission(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner)
        assert job["ov_task_id"]
        _, request = owner.ov.calls[-1]
        assert len(request["from"]) == 1
        directory = request["from"][0]
        assert directory in owner.ov.directories
        assert directory + "/packet.json" in owner.ov.contents

    asyncio.run(check())
