"""Unit tests for the OpenViking storage backend.

Mocks the OpenViking REST API at the ``httpx`` layer so the tests do not
require a real OpenViking server.  Covers:

- URI mapping onto the account-scoped, team-shared *resources* root
  (``viking://resources/{root_prefix}/...``, with an optional ``{group_id}``)
- ``put_object`` via ``POST /api/v1/content/write`` with the
  replace -> create -> append write strategy
- ``get_object`` via ``GET /api/v1/content/read``
- binary payload base64 fallback
- ``delete_object`` via ``DELETE /api/v1/fs``
- ``iter_objects`` via recursive ``GET /api/v1/fs/ls``
- ``build_object_store`` routes ``backend="viking"`` and aliases
- ``SkillHub.object_storage_from_config`` wires viking endpoint correctly
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.skills import SkillHub
from teamEvolver.storage import (
    OpenVikingObjectStore,
    build_object_store,
    is_not_found_error,
    normalize_backend,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeHttpx:
    """Minimal replacement for the httpx module used by OpenVikingObjectStore."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.handler = None  # type: ignore[var-annotated]

    def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        # Copy json/params so payload mutations after the call (e.g. retry
        # changing mode=replace -> mode=create) do not retroactively rewrite
        # captured history.
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            kwargs["json"] = dict(kwargs["json"])
        if "params" in kwargs and isinstance(kwargs["params"], dict):
            kwargs["params"] = dict(kwargs["params"])
        call = {"method": method, "url": url, **kwargs}
        self.calls.append(call)
        if self.handler is None:
            return _FakeResponse(200, {"status": "ok", "result": {}})
        return self.handler(call)


def _make_store(handler=None, **overrides) -> tuple[OpenVikingObjectStore, _FakeHttpx]:
    store = OpenVikingObjectStore(
        endpoint="http://viking.test",
        api_key="secret",
        account="acct",
        user="liuyue",
        agent="teamEvolver-evolve",
        agent_id=overrides.get("agent_id", "agent-1"),
        root_prefix=overrides.get("root_prefix", "teamEvolver"),
        group_id=overrides.get("group_id", ""),
    )
    fake = _FakeHttpx()
    fake.handler = handler
    store._httpx = fake
    return store, fake


# --------------------------------------------------------------------- #
# Pure helpers                                                           #
# --------------------------------------------------------------------- #


def test_normalize_backend_aliases_openviking_to_viking() -> None:
    assert normalize_backend("openviking") == "viking"
    assert normalize_backend("open-viking") == "viking"
    assert normalize_backend("open_viking") == "viking"
    assert normalize_backend("viking") == "viking"


def test_is_not_found_error_recognizes_openviking_tokens() -> None:
    assert is_not_found_error(RuntimeError("NOT_FOUND: missing"))
    assert is_not_found_error(RuntimeError("RESOURCE_NOT_FOUND: x"))
    assert is_not_found_error(RuntimeError("NoSuchURI: viking://...."))
    assert is_not_found_error(FileNotFoundError("anything"))
    assert not is_not_found_error(RuntimeError("INTERNAL_SERVER_ERROR: boom"))


def test_uri_uses_resources_namespace() -> None:
    store, _ = _make_store()
    assert (
        store._uri("peers/cust-a/sessions/x.json")
        == "viking://resources/teamEvolver/peers/cust-a/sessions/x.json"
    )


def test_uri_drops_leading_slash_and_normalizes_separators() -> None:
    store, _ = _make_store()
    assert store._uri("/skills/foo") == "viking://resources/teamEvolver/skills/foo"
    assert store._uri("skills\\foo") == "viking://resources/teamEvolver/skills/foo"


def test_uri_with_custom_root_prefix_and_group() -> None:
    store, _ = _make_store(root_prefix="teamEvolver", group_id="team-a")
    assert store._uri("skills/x.json") == "viking://resources/teamEvolver/team-a/skills/x.json"


# --------------------------------------------------------------------- #
# put / get round-trip                                                   #
# --------------------------------------------------------------------- #


def test_put_object_writes_utf8_content_and_uri() -> None:
    store, fake = _make_store(handler=lambda call: _FakeResponse(200, {"status": "ok"}))
    store.put_object("peers/cust-a/sessions/foo.json", b'{"hello":"world"}')

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://viking.test/api/v1/content/write"
    assert call["json"]["uri"] == "viking://resources/teamEvolver/peers/cust-a/sessions/foo.json"
    assert call["json"]["content"] == '{"hello":"world"}'
    assert call["json"]["mode"] == "replace"
    headers = call["headers"]
    assert headers["X-OpenViking-Account"] == "acct"
    assert headers["X-OpenViking-User"] == "liuyue"
    assert headers["X-OpenViking-Agent"] == "teamEvolver-evolve"
    assert headers["X-API-Key"] == "secret"
    assert headers["Authorization"] == "Bearer secret"


def test_put_object_falls_back_to_base64_for_binary_payload() -> None:
    store, fake = _make_store(handler=lambda call: _FakeResponse(200, {"status": "ok"}))
    binary = b"\xff\xfe\x80\x81\x82\x83"
    store.put_object("blobs/x.bin", binary)

    body = fake.calls[0]["json"]
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == binary


def test_put_object_falls_back_to_create_when_replace_misses() -> None:
    state = {"calls": 0}

    def handler(call: dict) -> _FakeResponse:
        del call
        state["calls"] += 1
        if state["calls"] == 1:
            return _FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "missing"}})
        return _FakeResponse(200, {"status": "ok"})

    store, fake = _make_store(handler=handler)
    store.put_object("skills/foo/SKILL.md", b"hello")

    assert state["calls"] == 2
    assert fake.calls[0]["json"]["mode"] == "replace"
    assert fake.calls[1]["json"]["mode"] == "create"


def test_put_object_falls_back_to_append_for_restricted_extension() -> None:
    state = {"calls": 0}

    def handler(call: dict) -> _FakeResponse:
        del call
        state["calls"] += 1
        if state["calls"] == 1:
            return _FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "x"}})
        if state["calls"] == 2:
            return _FakeResponse(
                400, {"error": {"code": "INVALID_ARGUMENT", "message": "extension not allowed"}}
            )
        return _FakeResponse(200, {"status": "ok"})

    store, fake = _make_store(handler=handler)
    store.put_object("peers/cust-a/sessions/foo.jsonl", b"line")

    assert state["calls"] == 3
    assert [c["json"]["mode"] for c in fake.calls] == ["replace", "create", "append"]


def test_get_object_reads_string_result() -> None:
    def handler(call: dict) -> _FakeResponse:
        assert call["url"].endswith("/api/v1/content/read")
        assert (
            call["params"]["uri"]
            == "viking://resources/teamEvolver/peers/cust-a/sessions/foo.json"
        )
        return _FakeResponse(200, {"status": "ok", "result": '{"x":1}'})

    store, _ = _make_store(handler=handler)
    obj = store.get_object("peers/cust-a/sessions/foo.json")

    assert obj.read() == b'{"x":1}'


def test_get_object_reads_dict_result_with_content_field() -> None:
    def handler(call: dict) -> _FakeResponse:
        del call
        return _FakeResponse(200, {"status": "ok", "result": {"content": "payload-text"}})

    store, _ = _make_store(handler=handler)
    assert store.get_object("k").read() == b"payload-text"


def test_get_object_translates_not_found_into_filenotfounderror() -> None:
    def handler(call: dict) -> _FakeResponse:
        del call
        return _FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "missing"}})

    store, _ = _make_store(handler=handler)
    with pytest.raises(FileNotFoundError):
        store.get_object("missing")


# --------------------------------------------------------------------- #
# delete via DELETE /api/v1/fs                                           #
# --------------------------------------------------------------------- #


def test_delete_object_issues_real_delete() -> None:
    store, fake = _make_store(handler=lambda call: _FakeResponse(200, {"status": "ok"}))
    store.delete_object("peers/cust-a/sessions/foo.json")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "http://viking.test/api/v1/fs"
    assert (
        call["params"]["uri"]
        == "viking://resources/teamEvolver/peers/cust-a/sessions/foo.json"
    )


def test_delete_object_swallows_not_found() -> None:
    def handler(call: dict) -> _FakeResponse:
        del call
        return _FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "x"}})

    store, _ = _make_store(handler=handler)
    # Must not raise.
    store.delete_object("missing")


# --------------------------------------------------------------------- #
# iter_objects via /api/v1/fs/ls                                         #
# --------------------------------------------------------------------- #


def test_iter_objects_walks_directory_tree() -> None:
    base = "viking://resources/teamEvolver/peers/cust-a"
    result = [
        {"uri": f"{base}/sessions", "isDir": True},
        {"uri": f"{base}/sessions/a.json", "isDir": False},
        {"uri": f"{base}/sessions/b.json", "isDir": False},
        {"uri": f"{base}/skills", "isDir": True},
    ]

    def handler(call: dict) -> _FakeResponse:
        assert call["url"].endswith("/api/v1/fs/ls")
        assert call["params"]["uri"] == f"{base}/"
        assert call["params"]["recursive"] == "true"
        return _FakeResponse(200, {"status": "ok", "result": result})

    store, _ = _make_store(handler=handler)
    keys = sorted(obj.key for obj in store.iter_objects(prefix="peers/cust-a"))
    assert keys == ["peers/cust-a/sessions/a.json", "peers/cust-a/sessions/b.json"]


def test_iter_objects_returns_empty_on_missing_prefix() -> None:
    def handler(call: dict) -> _FakeResponse:
        del call
        return _FakeResponse(404, {"error": {"code": "NOT_FOUND", "message": "x"}})

    store, _ = _make_store(handler=handler)
    assert list(store.iter_objects(prefix="peers/none")) == []


# --------------------------------------------------------------------- #
# build_object_store routing                                             #
# --------------------------------------------------------------------- #


def test_build_object_store_routes_viking_backend() -> None:
    store = build_object_store(
        backend="viking",
        endpoint="http://viking.test",
        viking_account="acct",
        viking_user="liuyue",
        viking_agent="teamEvolver",
        viking_agent_id="agent-1",
        viking_api_key="secret",
        viking_root_prefix="teamEvolver",
        viking_group_id="team-a",
    )
    assert isinstance(store, OpenVikingObjectStore)
    assert store._account == "acct"
    assert store._user == "liuyue"
    assert store._agent_id == "agent-1"
    assert store._root_prefix == "teamEvolver"
    assert store._group_id == "team-a"


def test_build_object_store_viking_defaults_root_prefix_and_group() -> None:
    store = build_object_store(backend="viking", endpoint="http://viking.test")
    assert isinstance(store, OpenVikingObjectStore)
    assert store._root_prefix == "team-skill-evolver"
    assert store._group_id == ""
    # Empty group => no group segment in the base URI.
    assert (
        store._uri("skills/x")
        == "viking://resources/team-skill-evolver/skills/x"
    )


def test_build_object_store_rejects_viking_without_endpoint() -> None:
    with pytest.raises(ValueError, match="OpenViking"):
        build_object_store(backend="viking")


def test_build_object_store_accepts_openviking_alias() -> None:
    store = build_object_store(backend="openviking", endpoint="http://viking.test")
    assert isinstance(store, OpenVikingObjectStore)


# --------------------------------------------------------------------- #
# Config plumbing                                                        #
# --------------------------------------------------------------------- #


def test_skill_hub_object_storage_from_config_builds_viking_bucket() -> None:
    cfg = TeamEvolverConfig(
        sharing_backend="viking",
        sharing_viking_endpoint="http://viking.test",
        sharing_viking_account="acct",
        sharing_viking_user="liuyue",
        sharing_viking_agent_id="agent-1",
        sharing_viking_customer_id="cust-a",
        sharing_viking_root_prefix="teamEvolver",
        sharing_viking_group_id="team-a",
    )

    hub = SkillHub.object_storage_from_config(cfg)
    assert hub is not None
    assert isinstance(hub._bucket, OpenVikingObjectStore)
    assert hub._bucket._user == "liuyue"
    assert hub._bucket._root_prefix == "teamEvolver"
    assert hub._bucket._group_id == "team-a"
