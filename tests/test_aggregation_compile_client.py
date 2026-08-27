from __future__ import annotations

import asyncio
import json

import httpx

from teamEvolver.aggregation.compile_client import CompileClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload


def test_http_transport_uploads_inline_skill_and_runs_compile_without_host_cli(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str, dict, dict]] = []
    poll_count = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json, headers):
            calls.append(("POST", url, json, headers))
            if url.endswith("/api/v1/skills"):
                return _FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "result": {
                            "uri": "viking://user/alice/skills/team-memory-okf"
                        },
                    },
                )
            assert url.endswith("/bot/v1/compile")
            return _FakeResponse(
                202,
                {
                    "status": "ok",
                    "result": {
                        "task_id": "cmp_1",
                        "status": "accepted",
                        "to": "viking://resources/team-memory",
                    },
                },
            )

        async def get(self, url, *, headers):
            nonlocal poll_count
            calls.append(("GET", url, {}, headers))
            poll_count += 1
            status = "running" if poll_count == 1 else "completed"
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "result": {
                        "task_id": "cmp_1",
                        "status": status,
                        "stage": "done" if status == "completed" else "agent",
                        "result": {"page_count": 2} if status == "completed" else None,
                    },
                },
            )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    monkeypatch.setattr("os.path.isfile", lambda _path: False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="alice",
        api_key="admin-secret",
        timeout_seconds=5,
    )
    skill_body = "---\nname: team-memory-okf\ndescription: Aggregate memory.\n---\n"

    async def run_scenario():
        installed = await client.install_skill(
            skill_name="team-memory-okf",
            skill_body=skill_body,
            parent_uri="viking://user/alice/skills",
        )
        compiled = await client.run_batch(
            source_uris=["viking://user/alice/memories/events"],
            target_uri="viking://resources/team-memory",
            skill_uri="viking://user/alice/skills/team-memory-okf",
            skill_revision="revision-1",
            runtime_timeout_seconds=123,
        )
        return installed, compiled

    installed, compiled = asyncio.run(run_scenario())

    assert installed["ok"] is True
    assert compiled["ok"] is True
    assert compiled["result"]["page_count"] == 2
    assert calls[0][1] == "http://openviking.example/api/v1/skills"
    assert calls[0][2]["data"] == skill_body
    assert calls[0][2]["target_uri"] == "viking://user/alice/skills"
    assert calls[1][1] == "http://openviking.example/bot/v1/compile"
    assert calls[1][2]["from"] == [
        "viking://user/alice/memories/events"
    ]
    assert calls[1][2]["skill_revision"] == "revision-1"
    assert "runtime_timeout_seconds" not in calls[1][2]
    assert all(call[3]["X-API-Key"] == "admin-secret" for call in calls)
    assert all(
        call[3]["X-OpenViking-Actor-Peer"] == "team-skill-evolver"
        for call in calls
    )
    assert "admin-secret" not in json.dumps(installed)
    assert "admin-secret" not in json.dumps(compiled)


def test_http_transport_returns_sanitized_upstream_error(monkeypatch) -> None:
    class UnauthorizedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json, headers):
            return _FakeResponse(
                401,
                {
                    "status": "error",
                    "error": {
                        "code": "UNAUTHENTICATED",
                        "message": "Admin Key rejected",
                    },
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", UnauthorizedClient)
    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="alice",
        api_key="admin-secret",
        timeout_seconds=5,
    )

    result = asyncio.run(
        client.install_skill(
            skill_name="team-memory-okf",
            skill_body="---\nname: team-memory-okf\n---\n",
            parent_uri="viking://user/alice/skills",
        )
    )

    assert result["ok"] is False
    assert result["exit_code"] == 401
    assert result["stderr"] == "Admin Key rejected"
    assert "admin-secret" not in json.dumps(result)


def test_publish_shared_skill_updates_once_and_returns_revision(monkeypatch) -> None:
    calls = []
    reads = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params, headers):
            nonlocal reads
            reads += 1
            calls.append(("GET", url, params))
            content = "old" if reads == 1 else "new"
            revision = "rev-old" if reads == 1 else "rev-new"
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "result": {
                        "root_uri": "viking://agent/skills/team-memory-okf",
                        "content": content,
                        "revision": revision,
                    },
                },
            )

        async def put(self, url, *, json, headers):
            calls.append(("PUT", url, json))
            return _FakeResponse(200, {"status": "ok", "result": {"action": "update"}})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="admin",
        api_key="admin-secret",
        timeout_seconds=5,
    )

    result = asyncio.run(
        client.publish_shared_skill(
            skill_name="team-memory-okf",
            skill_body="new",
            version_message="Update aggregation rules",
        )
    )

    assert result["ok"] is True
    assert result["result"]["revision"] == "rev-new"
    update = next(call for call in calls if call[0] == "PUT")
    assert update[2]["target_uri"] == "viking://agent/skills"
    assert update[2]["expected_revision"] == "rev-old"
    assert update[2]["version_message"] == "Update aggregation rules"


def test_publish_shared_skill_does_not_accept_user_skill_fallback(monkeypatch) -> None:
    calls = []
    installed = False

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params, headers):
            assert headers["X-OpenViking-Role"] == "admin"
            assert "X-OpenViking-Actor-Peer" not in headers
            calls.append(("GET", url, params))
            root_uri = (
                "viking://agent/skills/team-memory-okf"
                if installed
                else "viking://user/team/skills/team-memory-okf"
            )
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "result": {
                        "root_uri": root_uri,
                        "content": "shared body",
                        "revision": "shared-revision",
                    },
                },
            )

        async def post(self, url, *, json, headers):
            nonlocal installed
            assert headers["X-OpenViking-Role"] == "admin"
            assert "X-OpenViking-Actor-Peer" not in headers
            calls.append(("POST", url, json))
            installed = True
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "result": {
                        "root_uri": "viking://agent/skills/team-memory-okf",
                    },
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="team",
        api_key="root-secret",
        timeout_seconds=5,
    )

    result = asyncio.run(
        client.publish_shared_skill(
            skill_name="team-memory-okf",
            skill_body="shared body",
            version_message="Publish shared aggregation Skill",
        )
    )

    assert result["ok"] is True
    assert result["result"]["root_uri"] == "viking://agent/skills/team-memory-okf"
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][2]["target_uri"] == "viking://agent/skills"


def test_compile_submit_retries_transient_connection_failure(monkeypatch) -> None:
    attempts = 0

    class FlakyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json, headers):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError(
                    "temporary connection failure",
                    request=httpx.Request("POST", url),
                )
            return _FakeResponse(
                202,
                {
                    "status": "ok",
                    "result": {"task_id": "cmp_retry", "status": "accepted"},
                },
            )

        async def get(self, url, *, headers):
            return _FakeResponse(
                200,
                {
                    "status": "ok",
                    "result": {
                        "task_id": "cmp_retry",
                        "status": "completed",
                        "stage": "completed",
                        "result": {"page_count": 1},
                    },
                },
            )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FlakyClient)
    monkeypatch.setattr("asyncio.sleep", no_sleep)

    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="alice",
        api_key="admin-secret",
        timeout_seconds=5,
    )
    result = asyncio.run(
        client.run_batch(
            source_uris=["viking://user/alice/memories/events"],
            target_uri="viking://resources/team-memory",
            skill_uri="viking://user/alice/skills/team-memory-okf",
        )
    )

    assert result["ok"] is True
    assert result["result"]["page_count"] == 1
    assert attempts == 2


def test_stale_public_partition_delete(monkeypatch) -> None:
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def delete(self, url, *, params, headers):
            calls.append(("DELETE", url, params, headers))
            return _FakeResponse(
                200,
                {"status": "ok", "result": {"uri": params["uri"]}},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    client = CompileClient(
        endpoint="http://openviking.example",
        account_id="default",
        user_id="admin",
        api_key="admin-secret",
        timeout_seconds=5,
    )

    deleted = asyncio.run(
        client.delete_uri(
            uri="viking://resources/team-memory/partitions/ff",
        )
    )

    assert deleted["ok"] is True
    assert calls[0][2]["recursive"] == "true"
