from __future__ import annotations

import hashlib
from typing import Any

import pytest

from teamEvolver.storage.snapshot import (
    OpenVikingSnapshotClient,
    SnapshotConflictError,
    SnapshotProtocolError,
)

OID_A = "a" * 40
OID_B = "b" * 40


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content
        self.headers = dict(headers or {})
        self.text = text

    def json(self) -> Any:
        return self._payload


class _HTTP:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.responses.pop(0)


def _client(http: _HTTP) -> OpenVikingSnapshotClient:
    return OpenVikingSnapshotClient(
        endpoint="https://openviking.test",
        account="tenant-a",
        user="team",
        branch="main",
        http_client=http,
    )


def test_snapshot_commit_and_log_use_scoped_paths() -> None:
    http = _HTTP(
        [
            _Response(
                payload={
                    "status": "ok",
                    "result": {
                        "result": "created",
                        "commit_oid": OID_A,
                        "changed": 1,
                    },
                }
            ),
            _Response(
                payload={
                    "status": "ok",
                    "result": [{"oid": OID_A, "message": "before"}],
                }
            ),
        ]
    )
    client = _client(http)
    path = "viking://user/memories/pattern/a.md"

    result = client.commit(
        message="before",
        paths=[path, path],
    )
    history = client.log(limit=5, paths=[path])

    assert result["commit_oid"] == OID_A
    assert history[0]["oid"] == OID_A
    assert http.calls[0]["json"]["paths"] == [path]
    assert ("paths", path) in http.calls[1]["params"]
    assert client.account_hash == hashlib.sha256(b"tenant-a").hexdigest()


def test_snapshot_show_diff_and_restore_are_read_only_surfaces() -> None:
    content = b"visible memory"
    http = _HTTP(
        [
            _Response(
                content=content,
                headers={
                    "X-Snapshot-Oid": OID_A,
                    "X-Snapshot-Size": str(len(content)),
                },
            ),
            _Response(
                payload={
                    "status": "ok",
                    "result": {
                        "change_type": "modified",
                        "diff_text": "-old\n+new\n",
                    },
                }
            ),
            _Response(
                payload={
                    "status": "ok",
                    "result": {"result": "dry_run", "diff": {}},
                }
            ),
        ]
    )
    client = _client(http)
    path = "viking://user/memories/pattern/a.md"

    blob = client.show_blob(OID_A, path=path, raw=False)
    diff = client.diff(
        path=path,
        from_ref=OID_A,
        to_ref=OID_B,
        raw=False,
    )
    restore = client.restore_dry_run(
        source_commit=OID_A,
        project_dir="viking://user/memories/pattern",
    )

    assert blob.content == content
    assert blob.sha256 == hashlib.sha256(content).hexdigest()
    assert diff["change_type"] == "modified"
    assert restore["result"] == "dry_run"
    assert http.calls[0]["params"]["raw"] == "false"
    assert http.calls[2]["json"]["dry_run"] is True
    assert not hasattr(client, "restore")


def test_snapshot_maps_concurrent_commit_to_typed_error() -> None:
    http = _HTTP(
        [
            _Response(
                status_code=409,
                payload={
                    "status": "error",
                    "error": {
                        "code": "CONFLICT",
                        "message": "branch changed",
                    },
                },
            )
        ]
    )

    with pytest.raises(SnapshotConflictError) as error:
        _client(http).commit(message="conflict")

    assert error.value.code == "CONFLICT"
    assert error.value.status_code == 409


def test_snapshot_rejects_malformed_server_oid() -> None:
    http = _HTTP(
        [
            _Response(
                payload={
                    "status": "ok",
                    "result": {
                        "result": "created",
                        "commit_oid": "short",
                    },
                }
            )
        ]
    )

    with pytest.raises(SnapshotProtocolError, match="full OID"):
        _client(http).commit(message="bad response")
