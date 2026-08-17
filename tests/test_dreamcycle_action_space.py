from __future__ import annotations

import base64
import json

from teamEvolver.dreamcycle.blackboard import Blackboard
from teamEvolver.dreamcycle.config import OpenVikingConfig
from teamEvolver.dreamcycle.tools.blackboard_tool import BlackboardTool
from teamEvolver.dreamcycle.tools.viking import (
    VikingForgetTool,
    VikingHTTPClient,
    VikingMergeTool,
    VikingReadManyTool,
    VikingRememberTool,
)


def _key(account: str, user: str) -> str:
    encode = lambda value: base64.b64encode(value.encode()).decode().rstrip("=")
    return f"{encode(account)}.{encode(user)}.secret"


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _ScriptedClient(VikingHTTPClient):
    def __init__(
        self,
        user="team",
        *,
        write_status=200,
        mv_status=200,
        read_payload="body",
    ):
        config = OpenVikingConfig(api_key=_key("acct", user))
        super().__init__(config)
        self._write_status = write_status
        self._mv_status = mv_status
        self._read_payload = read_payload
        self.writes = []
        self.moves = []

    def get(self, path, **kwargs):
        if "/content/read" in path:
            return _Response(200, {"result": self._read_payload})
        return _Response(200, {"result": []})

    def post(self, path, **kwargs):
        body = kwargs.get("json", {})
        if "/content/write" in path:
            self.writes.append(body)
            return _Response(self._write_status, text="write-fail")
        if "/fs/mv" in path:
            self.moves.append(body)
            return _Response(self._mv_status, text="mv-fail")
        return _Response(404)


class _Ledger:
    def __init__(self) -> None:
        self.prepared: list[dict] = []
        self.finished: list[dict] = []

    def prepare(self, **kwargs):
        self.prepared.append(kwargs)
        return {"change": len(self.prepared)}

    def finish(self, token, **kwargs):
        self.finished.append({"token": token, **kwargs})
        return {
            "change_id": "mch-test",
            "action": self.prepared[-1]["action"],
            "result": kwargs["result"],
        }


def test_merge_writes_survivor_then_archives_sources() -> None:
    client = _ScriptedClient()
    blackboard = Blackboard()
    ledger = _Ledger()
    tool = VikingMergeTool(
        client,
        blackboard=blackboard,
        change_ledger=ledger,
    )
    target = "viking://user/memories/pattern/keep.md"
    sources = [
        "viking://user/memories/pattern/dup1.md",
        "viking://user/memories/pattern/dup2.md",
        target,
    ]

    result = tool.execute(
        target_uri=target,
        content="merged body",
        source_uris=sources,
        reason="dedupe",
    )

    assert result.success is True
    assert len(client.writes) == 1
    assert client.writes[0]["uri"] == target
    assert [move["from_uri"] for move in client.moves] == sources[:2]
    assert blackboard.is_processed(sources[0])
    assert blackboard.is_processed(sources[1])
    assert not blackboard.is_processed(target)
    assert ledger.prepared[0]["action"] == "merge"
    assert ledger.finished[0]["result"] == "applied"
    assert result.metadata["memory_change"]["change_id"] == "mch-test"


def test_remember_and_forget_emit_memory_change_records() -> None:
    client = _ScriptedClient()
    ledger = _Ledger()
    target = "viking://user/memories/pattern/keep.md"

    remembered = VikingRememberTool(
        client,
        change_ledger=ledger,
    ).execute(
        content="durable team fact",
        target_uri=target,
    )
    forgotten = VikingForgetTool(
        client,
        change_ledger=ledger,
    ).execute(
        uri=target,
        reason="superseded",
    )

    assert remembered.success is True
    assert forgotten.success is True
    assert [item["action"] for item in ledger.prepared] == [
        "update",
        "archive",
    ]
    assert [item["result"] for item in ledger.finished] == [
        "applied",
        "applied",
    ]


def test_merge_aborts_when_survivor_write_fails() -> None:
    client = _ScriptedClient(write_status=500)
    result = VikingMergeTool(client).execute(
        target_uri="viking://user/memories/pattern/keep.md",
        content="x",
        source_uris=["viking://user/memories/pattern/dup.md"],
    )

    assert result.success is False
    assert client.moves == []


def test_merge_rejects_source_outside_memory_space() -> None:
    client = _ScriptedClient()
    result = VikingMergeTool(client).execute(
        target_uri="viking://user/memories/pattern/keep.md",
        content="x",
        source_uris=["viking://user/skills/other.md"],
    )

    assert result.success is False
    assert client.writes == []


def test_read_many_batches_and_reports_partial() -> None:
    client = _ScriptedClient(read_payload="doc-body")
    tool = VikingReadManyTool(client)
    uris = [
        "viking://user/memories/pattern/a.md",
        "viking://user/skills/forbidden.md",
    ]

    result = tool.execute(uris=uris)
    documents = json.loads(result.output)

    assert documents[uris[0]]["success"] is True
    assert documents[uris[1]]["success"] is False
    assert result.metadata["read"] == 1


def test_blackboard_shares_facts_and_processed_across_jobs() -> None:
    blackboard = Blackboard()
    tool = BlackboardTool(blackboard)

    tool.execute(action="record_fact", topic="members", value="alice - PM")
    tool.execute(
        action="mark_processed",
        uri="viking://user/memories/pattern/x.md",
        note="archived",
    )

    recalled = json.loads(tool.execute(action="recall").output)
    assert "alice - PM" in recalled["facts"]["members"]
    assert "viking://user/memories/pattern/x.md" in recalled["processed"]
