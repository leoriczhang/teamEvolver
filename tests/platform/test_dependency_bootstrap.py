"""Dependency bootstrap contracts shared by runtime features."""

import asyncio

from teamEvolver.admin.openviking_dirs import _dir_uris, _expand_ancestors
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.openviking import OpenVikingResult, ensure_directories


def test_openviking_bootstrap_includes_all_builtin_feature_roots():
    directories = set(
        _expand_ancestors(
            _dir_uris(TeamEvolverConfig(), [("alice", "alice")])
        )
    )

    assert {
        "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base",
        "viking://resources/agent_knowledge_workspace/input/proven_experiences",
        "viking://resources/agent_knowledge_workspace/output/processed_knowledge",
        "viking://resources/shared-knowledge",
        "viking://resources/team-skill-evolver/skills",
        "viking://user/alice/memories",
        "viking://user/alice/resources",
        "viking://user/alice/skills",
    } <= directories


def test_directory_initializer_reuses_existing_directories():
    class ExistingDirectoryClient:
        def __init__(self):
            self.requested = []

        async def request(self, method, path, **kwargs):
            self.requested.append(kwargs["json_body"]["uri"])
            return OpenVikingResult(False, 409, error="ALREADY_EXISTS")

    client = ExistingDirectoryClient()
    result = asyncio.run(
        ensure_directories(
            client,
            ["viking://resources/feature/input/raw"],
        )
    )

    assert result == [
        "viking://resources/feature",
        "viking://resources/feature/input",
        "viking://resources/feature/input/raw",
    ]
    assert client.requested == result
