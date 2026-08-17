from __future__ import annotations

import base64
import json

from teamEvolver.dreamcycle.config import OpenVikingConfig
from teamEvolver.dreamcycle.tools.viking import (
    VikingReadTool,
    VikingSearchTool,
)


def _key(account: str, user: str) -> str:
    def encode(value: str) -> str:
        return base64.b64encode(value.encode()).decode().rstrip("=")

    return f"{encode(account)}.{encode(user)}.secret"


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, user: str) -> None:
        self.user = user
        self.calls: list[tuple[str, dict]] = []

    def post(self, _path: str, **kwargs):
        self.calls.append(("post", kwargs))
        return _Response(
            {
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/memories/pattern/x.md",
                            "abstract": self.user,
                        }
                    ],
                    "resources": [],
                    "skills": [],
                    "total": 1,
                }
            }
        )

    def get(self, _path: str, **kwargs):
        self.calls.append(("get", kwargs))
        return _Response({"result": f"content-from-{self.user}"})


def test_target_and_personal_sources_are_separate() -> None:
    target_key = _key("acct", "team")
    alice_key = _key("acct", "alice")
    config = OpenVikingConfig(
        api_key=target_key,
        source_api_keys=[alice_key, target_key],
    )

    assert config.account == "acct"
    assert config.agent_id == "team"
    assert config.source_api_keys == [alice_key]


def test_local_root_key_can_use_explicit_source_users() -> None:
    config = OpenVikingConfig(
        api_key="local-root",
        agent_id="team",
        source_users=["alice", "bob", "team"],
    )

    assert config.agent_id == "team"
    assert config.source_users == ["alice", "bob"]


def test_search_and_read_route_through_each_personal_client() -> None:
    target = _Client("team")
    alice = _Client("alice")
    search = VikingSearchTool(target, source_clients=[alice])

    result = search.execute(query="workflow", scope="all", limit=10)

    assert result.success is True
    assert len(target.calls) == len(alice.calls) == 1
    items = json.loads(result.output)
    alice_item = next(
        item for item in items if item["source_user"] == "alice"
    )
    assert alice_item["uri"].startswith("viking://user/alice/")

    read = VikingReadTool(target, source_clients=[alice])
    read_result = read.execute(uri=alice_item["uri"])

    assert read_result.success is True
    assert read_result.output == "content-from-alice"
