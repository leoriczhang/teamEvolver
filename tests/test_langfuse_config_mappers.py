"""Tests for the per-agent mapper registry config layer.

Covers:
- ConfigStore bridging: legacy migration (mappers key absent vs explicit []),
  mappers-wins-when-both-present, and normalization passthrough.
- Route-layer registry validation helper (duplicate/empty names, broken code).
"""

from __future__ import annotations

import pytest

from teamEvolver.config_store.bridge import ConfigStore
from teamEvolver.proxy.routes import _validate_mapper_registry_entries

_MAP_OK = "def map_trace(t, o):\n    return {'response_text': 'x'}"


def _store_with(data: dict) -> ConfigStore:
    store = ConfigStore()
    store.load = lambda: data  # type: ignore[method-assign]
    return store


# --------------------------------------------------------------------------- #
# Bridge migration                                                             #
# --------------------------------------------------------------------------- #
def test_bridge_migrates_legacy_single_mapper() -> None:
    store = _store_with(
        {"langfuse": {"mapper_enabled": True, "mapper_code": _MAP_OK}}
    )
    cfg = store.to_config()
    assert len(cfg.langfuse_mappers) == 1
    entry = cfg.langfuse_mappers[0]
    assert entry["name"] == "default"
    assert entry["enabled"] is True
    assert entry["code"] == _MAP_OK
    assert entry["match"] == {"trace_names": [], "tags": [], "session_id_patterns": []}


def test_bridge_explicit_empty_list_disables_legacy() -> None:
    store = _store_with(
        {"langfuse": {"mapper_enabled": True, "mapper_code": _MAP_OK, "mappers": []}}
    )
    cfg = store.to_config()
    assert cfg.langfuse_mappers == []


def test_bridge_mappers_win_over_legacy() -> None:
    store = _store_with(
        {
            "langfuse": {
                "mapper_enabled": True,
                "mapper_code": "old",
                "mappers": [{"name": "x", "enabled": True, "code": _MAP_OK}],
            }
        }
    )
    cfg = store.to_config()
    assert [e["name"] for e in cfg.langfuse_mappers] == ["x"]


def test_bridge_no_langfuse_section() -> None:
    store = _store_with({})
    assert store.to_config().langfuse_mappers == []


# --------------------------------------------------------------------------- #
# Route-layer registry validation                                              #
# --------------------------------------------------------------------------- #
def test_validate_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="列表"):
        _validate_mapper_registry_entries({"name": "x"})


def test_validate_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="name"):
        _validate_mapper_registry_entries([{"enabled": True, "code": _MAP_OK}])


def test_validate_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="重复"):
        _validate_mapper_registry_entries(
            [
                {"name": "a", "enabled": True, "code": _MAP_OK},
                {"name": "a", "enabled": False, "code": ""},
            ]
        )


def test_validate_rejects_enabled_entry_with_broken_code() -> None:
    with pytest.raises(ValueError, match="无法启用"):
        _validate_mapper_registry_entries(
            [{"name": "bad", "enabled": True, "code": "def nope(: bad"}]
        )


def test_validate_rejects_enabled_entry_with_empty_code() -> None:
    with pytest.raises(ValueError, match="代码为空"):
        _validate_mapper_registry_entries([{"name": "empty", "enabled": True, "code": ""}])


def test_validate_accepts_disabled_entry_with_empty_code() -> None:
    entries = _validate_mapper_registry_entries(
        [{"name": "draft", "enabled": False, "code": ""}]
    )
    assert len(entries) == 1
    assert entries[0]["enabled"] is False


def test_validate_normalizes_and_returns_entries() -> None:
    entries = _validate_mapper_registry_entries(
        [
            {
                "name": " agent-a ",
                "enabled": True,
                "code": _MAP_OK,
                "match": {"tags": "foo, bar"},
            }
        ]
    )
    assert entries[0]["name"] == "agent-a"
    assert entries[0]["match"]["tags"] == ["foo", "bar"]
