"""Tests for the user-authorable Langfuse trace mapper.

Covers:
- compile guardrails (syntax, missing entry point, disabled import/open),
- the (trace, observations) -> standard-format turn contract and deep-merge,
- build-from-config gating + fail-open when code is broken,
- the pull pipeline honoring a configured mapper (and falling back on error),
- the dry-run preview helper used by the console tester, exercised against the
  real Langfuse export shape in ``/home/zhangpengkun/traces/1.json`` when present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations import langfuse_pull
from teamEvolver.integrations import source_adapter
from teamEvolver.integrations.langfuse_convert import (
    convert_langfuse_session,
    convert_trace_to_turn,
)
from teamEvolver.integrations.langfuse_mapper import (
    MapperError,
    TraceMapper,
    build_mapper_registry,
    build_trace_mapper_from_config,
    compile_mapper,
    default_mapper_code,
    normalize_trace_input,
    run_mapper_preview,
    sample_trace_payload,
    standard_format_spec,
)
from teamEvolver.integrations.langfuse_mapper import TURN_KEYS


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
_REAL_TRACE_FILE = Path("/home/zhangpengkun/traces/1.json")


def _trace(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": "t1",
        "name": "agent-run",
        "input": "帮我跑一下日报脚本",
        "output": "已执行完成",
        "tags": ["agent"],
        "metadata": {"usage": {"input": 100, "output": 20, "total": 120}, "success": True},
        "observations": [
            {"id": "g1", "type": "GENERATION", "name": "LLM Request", "startTime": "2026-01-01T00:00:01Z"},
            {"id": "s1", "type": "SPAN", "name": "tool: exec", "startTime": "2026-01-01T00:00:02Z"},
        ],
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------- #
# compile_mapper guardrails                                                    #
# --------------------------------------------------------------------------- #
def test_compile_mapper_returns_entry_point() -> None:
    fn = compile_mapper("def map_trace(trace, observations):\n    return {}")
    assert callable(fn)


def test_compile_mapper_accepts_map_turn_alias() -> None:
    fn = compile_mapper("def map_turn(trace, observations):\n    return {}")
    assert callable(fn)


def test_compile_mapper_rejects_empty() -> None:
    with pytest.raises(MapperError):
        compile_mapper("   ")


def test_compile_mapper_rejects_syntax_error() -> None:
    with pytest.raises(MapperError):
        compile_mapper("def map_trace(t, o)\n    return {}")


def test_compile_mapper_requires_entry_point() -> None:
    with pytest.raises(MapperError):
        compile_mapper("def helper(t, o):\n    return {}")


def test_compile_mapper_blocks_import() -> None:
    with pytest.raises(MapperError):
        compile_mapper("import os\ndef map_trace(t, o):\n    return {}")


def test_mapper_runtime_blocks_open() -> None:
    # open() is not importable/available; the failure surfaces at call time.
    mapper = TraceMapper.from_code(
        "def map_trace(t, o):\n    open('/etc/passwd')\n    return {}"
    )
    with pytest.raises(Exception):
        mapper(_trace(), [], 1, {"turn_num": 1})


# --------------------------------------------------------------------------- #
# TraceMapper contract + deep merge                                            #
# --------------------------------------------------------------------------- #
def test_mapper_partial_result_deep_merges_over_builtin() -> None:
    trace = _trace()
    builtin = convert_trace_to_turn(trace, 1)
    mapper = TraceMapper.from_code(
        "def map_trace(trace, observations):\n"
        "    return {'metrics': {'tool_call_count': 7}, '_langfuse': {'success': False}}"
    )
    merged = convert_trace_to_turn(trace, 1, mapper=mapper)
    # Overridden nested keys win...
    assert merged["metrics"]["tool_call_count"] == 7
    assert merged["_langfuse"]["success"] is False
    # ...while sibling keys are inherited from the built-in turn.
    assert merged["metrics"]["input_tokens"] == builtin["metrics"]["input_tokens"]
    assert merged["prompt_text"] == builtin["prompt_text"]
    assert merged["_langfuse"]["trace_name"] == builtin["_langfuse"]["trace_name"]


def test_mapper_returning_none_yields_builtin() -> None:
    trace = _trace()
    builtin = convert_trace_to_turn(trace, 1)
    mapper = TraceMapper.from_code("def map_trace(trace, observations):\n    return None")
    assert convert_trace_to_turn(trace, 1, mapper=mapper) == builtin


def test_mapper_turn_num_defaults_to_pipeline_index() -> None:
    mapper = TraceMapper.from_code("def map_trace(t, o):\n    return {'prompt_text': 'x'}")
    turn = convert_trace_to_turn(_trace(), 5, mapper=mapper)
    assert turn["turn_num"] == 5


def test_mapper_may_override_turn_num_with_positive_int() -> None:
    mapper = TraceMapper.from_code("def map_trace(t, o):\n    return {'turn_num': 9}")
    turn = convert_trace_to_turn(_trace(), 5, mapper=mapper)
    assert turn["turn_num"] == 9


def test_mapper_rejects_non_dict_result() -> None:
    mapper = TraceMapper.from_code("def map_trace(t, o):\n    return 42")
    with pytest.raises(MapperError):
        convert_trace_to_turn(_trace(), 1, mapper=mapper)


def test_mapper_receives_observations_argument() -> None:
    # A mapper that counts only tool spans, using the observations arg directly.
    mapper = TraceMapper.from_code(
        "def map_trace(trace, observations):\n"
        "    tools = [o for o in observations if str(o.get('name','')).startswith('tool:')]\n"
        "    return {'metrics': {'tool_call_count': len(tools)}}"
    )
    turn = convert_trace_to_turn(_trace(), 1, mapper=mapper)
    assert turn["metrics"]["tool_call_count"] == 1


def test_mapper_adapts_to_positional_only_param_names() -> None:
    mapper = TraceMapper.from_code(
        "def map_trace(a, b):\n    return {'response_text': str(a.get('output'))}"
    )
    turn = convert_trace_to_turn(_trace(output="POS"), 1, mapper=mapper)
    assert turn["response_text"] == "POS"


def test_mapper_adapts_to_kwargs_signature() -> None:
    mapper = TraceMapper.from_code(
        "def map_trace(**kw):\n    return {'prompt_text': str(kw['trace'].get('input'))}"
    )
    turn = convert_trace_to_turn(_trace(input="KW"), 1, mapper=mapper)
    assert turn["prompt_text"] == "KW"


def test_mapper_adapts_to_varargs_signature() -> None:
    mapper = TraceMapper.from_code(
        "def map_trace(*a):\n    return {'response_text': str(a[0].get('output'))}"
    )
    turn = convert_trace_to_turn(_trace(output="VA"), 1, mapper=mapper)
    assert turn["response_text"] == "VA"


def test_mapper_adapts_to_reordered_named_params() -> None:
    mapper = TraceMapper.from_code(
        "def map_trace(observations, trace):\n"
        "    return {'metrics': {'tool_call_count': len(observations)}}"
    )
    turn = convert_trace_to_turn(_trace(), 1, mapper=mapper)
    # _trace() has two observations.
    assert turn["metrics"]["tool_call_count"] == 2


def test_mapper_receives_turn_num_and_defaults_when_declared() -> None:
    mapper = TraceMapper.from_code(
        "def map_trace(trace, observations, turn_num, defaults):\n"
        "    return {'trace_id': str(turn_num) + ':' + str(defaults.get('trace_id'))}"
    )
    turn = convert_trace_to_turn(_trace(id="tX"), 4, mapper=mapper)
    assert turn["trace_id"] == "4:tX"


# --------------------------------------------------------------------------- #
# build_trace_mapper_from_config gating (legacy fields → catch-all registry)   #
# --------------------------------------------------------------------------- #
def test_build_mapper_disabled_returns_none() -> None:
    cfg = TeamEvolverConfig(langfuse_mapper_enabled=False, langfuse_mapper_code="def map_trace(t,o): return {}")
    assert build_trace_mapper_from_config(cfg) is None


def test_build_mapper_empty_code_returns_none() -> None:
    cfg = TeamEvolverConfig(langfuse_mapper_enabled=True, langfuse_mapper_code="")
    assert build_trace_mapper_from_config(cfg) is None


def test_build_mapper_broken_code_fails_open() -> None:
    cfg = TeamEvolverConfig(langfuse_mapper_enabled=True, langfuse_mapper_code="def nope(: bad")
    # Compile failure is swallowed so a broken mapper never blocks a pull: the
    # registry carries the entry in ``broken`` and can never route.
    registry = build_trace_mapper_from_config(cfg)
    assert registry is not None
    assert [name for name, _ in registry.broken] == ["default"]
    from teamEvolver.integrations.langfuse_mapper import MapperRegistry

    assert isinstance(registry, MapperRegistry)
    assert registry.for_trace(_trace(id="t1")) is None


def test_build_mapper_enabled_valid_code_returns_mapper() -> None:
    from teamEvolver.integrations.langfuse_mapper import MapperRegistry

    cfg = TeamEvolverConfig(
        langfuse_mapper_enabled=True,
        langfuse_mapper_code="def map_trace(t, o):\n    return {'prompt_text': 'x'}",
    )
    assert isinstance(build_trace_mapper_from_config(cfg), MapperRegistry)


# --------------------------------------------------------------------------- #
# Session-level aggregation with a custom mapper                               #
# --------------------------------------------------------------------------- #
def test_convert_session_applies_mapper_and_still_aggregates() -> None:
    session = {"id": "s1", "createdAt": "2026-01-01T00:00:00Z"}
    traces = [_trace(id="t1"), _trace(id="t2")]
    mapper = TraceMapper.from_code(
        "def map_trace(t, o):\n    return {'metrics': {'tool_call_count': 3, 'total_tokens': 10}}"
    )
    converted = convert_langfuse_session(session, traces, mapper=mapper)
    assert len(converted["turns"]) == 2
    # Session metrics sum the per-turn (mapper-produced) values.
    assert converted["metrics"]["tool_call_count"] == 6
    assert converted["metrics"]["total_tokens"] == 20


def test_convert_session_tolerates_mapper_dropping_metrics() -> None:
    session = {"id": "s1"}
    traces = [_trace(id="t1")]
    # A mapper that replaces ``metrics`` with a non-dict must not crash the
    # session-level aggregation; such turns contribute 0 to the totals.
    mapper = TraceMapper.from_code("def map_trace(t, o):\n    return {'metrics': 'oops'}")
    converted = convert_langfuse_session(session, traces, mapper=mapper)
    assert converted["metrics"]["tool_call_count"] == 0
    assert converted["metrics"]["total_tokens"] == 0


# --------------------------------------------------------------------------- #
# Pull pipeline integration                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_pull_sessions_uses_configured_mapper(monkeypatch) -> None:
    """Adapter map_trace hook overrides the built-in turn mapping."""
    from teamEvolver.integrations.source_adapter import AgentHooks

    config = TeamEvolverConfig(
        langfuse_enabled=True,
        langfuse_max_sessions=10,
    )

    class _PullFakeClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def list_session_ids(self, filters, *, max_sessions=0):
            return ["s1"]

        def fetch_session_with_traces(self, session_id, trace_name=""):
            return {"id": "s1"}, [_trace(id="t1", input="valuable work")]

    monkeypatch.setattr(langfuse_pull, "LangfuseClient", _PullFakeClient)
    monkeypatch.setattr(source_adapter, "LangfuseClient", _PullFakeClient)

    # Inject a file-based adapter hook that overrides map_trace.
    def _custom_map_trace(trace, observations, turn_num=0, defaults=None):
        return {"response_text": "MAPPED", "metrics": {"tool_call_count": 42}}

    monkeypatch.setattr(
        source_adapter,
        "resolve_hooks",
        lambda agent_id, config=None: AgentHooks(
            map_trace=_custom_map_trace,
            defined={"map_trace"},
        ),
    )

    ingested: list[dict[str, Any]] = []

    async def _ingest(session: dict[str, Any]) -> dict[str, Any]:
        ingested.append(session)
        return {"status": "queued", "queued": True}

    result = await langfuse_pull.pull_sessions(
        config, _ingest, {}, max_sessions=10, agent_id="test-agent"
    )

    assert result["counts"]["queued"] == 1
    assert len(ingested) == 1
    turn = ingested[0]["turns"][0]
    assert turn["response_text"] == "MAPPED"
    assert turn["metrics"]["tool_call_count"] == 42


@pytest.mark.anyio
async def test_pull_sessions_falls_back_when_mapper_raises(monkeypatch) -> None:
    """Adapter map_trace that raises falls back to the built-in mapping."""
    from teamEvolver.integrations.source_adapter import AgentHooks

    config = TeamEvolverConfig(
        langfuse_enabled=True,
        langfuse_max_sessions=10,
    )

    class _PullFakeClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def list_session_ids(self, filters, *, max_sessions=0):
            return ["s1"]

        def fetch_session_with_traces(self, session_id, trace_name=""):
            return {"id": "s1"}, [_trace(id="t1", input="valuable work")]

    monkeypatch.setattr(langfuse_pull, "LangfuseClient", _PullFakeClient)
    monkeypatch.setattr(source_adapter, "LangfuseClient", _PullFakeClient)

    # Inject a broken map_trace that raises — should fall back to built-in.
    def _broken_map_trace(trace, observations, turn_num=0, defaults=None):
        raise ValueError("boom")

    monkeypatch.setattr(
        source_adapter,
        "resolve_hooks",
        lambda agent_id, config=None: AgentHooks(
            map_trace=_broken_map_trace,
            defined={"map_trace"},
        ),
    )

    ingested: list[dict[str, Any]] = []

    async def _ingest(session: dict[str, Any]) -> dict[str, Any]:
        ingested.append(session)
        return {"status": "queued", "queued": True}

    result = await langfuse_pull.pull_sessions(
        config, _ingest, {}, max_sessions=10, agent_id="test-agent"
    )

    # Session still ingests via the built-in mapping (no crash, not empty).
    assert result["counts"]["queued"] == 1
    assert ingested[0]["turns"][0]["response_text"] == "已执行完成"


# --------------------------------------------------------------------------- #
# Dry-run preview + input normalization                                        #
# --------------------------------------------------------------------------- #
def test_normalize_trace_input_accepts_wrapped_and_bare() -> None:
    wrapped = {"trace": {"id": "t1"}, "observations": [{"id": "o1"}]}
    trace, obs = normalize_trace_input(wrapped)
    assert trace["id"] == "t1"
    assert obs == [{"id": "o1"}]

    bare = {"id": "t2", "observations": [{"id": "o2"}]}
    trace, obs = normalize_trace_input(bare)
    assert trace["id"] == "t2"
    assert obs == [{"id": "o2"}]


def test_normalize_trace_input_rejects_non_object() -> None:
    with pytest.raises(MapperError):
        normalize_trace_input([1, 2, 3])


def test_run_mapper_preview_success_returns_turn_and_builtin() -> None:
    res = run_mapper_preview(default_mapper_code(), sample_trace_payload())
    assert res["ok"] is True
    assert "turn" in res and "builtin" in res
    assert res["observation_count"] == 3
    # Default template maps the sample prompt/response text.
    assert "脚本" in res["turn"]["response_text"]


def test_run_mapper_preview_reports_compile_error() -> None:
    res = run_mapper_preview("def broken(: ", sample_trace_payload())
    assert res["ok"] is False
    assert "syntax error" in res["error"]


def test_run_mapper_preview_reports_runtime_error() -> None:
    res = run_mapper_preview(
        "def map_trace(t, o):\n    return t['does_not_exist']", sample_trace_payload()
    )
    assert res["ok"] is False
    assert "mapper raised" in res["error"]


def test_default_template_compiles_and_runs() -> None:
    mapper = TraceMapper.from_code(default_mapper_code())
    turn = convert_trace_to_turn(_trace(), 1, mapper=mapper)
    assert isinstance(turn, dict)
    assert turn["turn_num"] == 1


# --------------------------------------------------------------------------- #
# Real export file (the shape the customer handed us)                          #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _REAL_TRACE_FILE.exists(), reason="sample trace file absent")
def test_real_export_maps_with_default_template() -> None:
    payload = json.loads(_REAL_TRACE_FILE.read_text(encoding="utf-8"))
    res = run_mapper_preview(default_mapper_code(), payload)
    assert res["ok"] is True
    turn = res["turn"]
    # The real trace carries usage + one exec tool span + two generations.
    assert turn["metrics"]["input_tokens"] == 533
    assert turn["metrics"]["total_tokens"] == 3180
    assert turn["metrics"]["tool_call_count"] == 1
    assert turn["metrics"]["api_call_count"] == 2
    assert turn["_langfuse"]["trace_name"] == "openclaw-turn"


@pytest.mark.skipif(not _REAL_TRACE_FILE.exists(), reason="sample trace file absent")
def test_real_export_builtin_and_custom_differ_only_where_expected() -> None:
    payload = json.loads(_REAL_TRACE_FILE.read_text(encoding="utf-8"))
    trace, _obs = normalize_trace_input(payload)
    builtin = convert_trace_to_turn(trace, 1)
    # Built-in counts spans (agent loops + tool) as tool activity; the default
    # template counts only ``tool:`` spans, so the two intentionally differ.
    assert builtin["metrics"]["tool_call_count"] >= 1
    res = run_mapper_preview(default_mapper_code(), payload)
    assert res["turn"]["metrics"]["tool_call_count"] == 1


# --------------------------------------------------------------------------- #
# Standard-format spec (documented in the console dialog)                      #
# --------------------------------------------------------------------------- #
def test_standard_format_spec_covers_all_turn_keys() -> None:
    spec = standard_format_spec()
    field_keys = {f["key"] for f in spec["fields"]}
    assert field_keys == set(TURN_KEYS)
    # The worked example is itself a valid, complete turn shape.
    assert set(spec["example"].keys()) == set(TURN_KEYS)


def test_standard_format_spec_is_json_serializable() -> None:
    spec = standard_format_spec()
    dumped = json.dumps(spec, ensure_ascii=False)
    assert "prompt_text" in dumped
    assert spec["title"]
    assert spec["summary"]


def test_standard_format_example_maps_through_builtin_shape() -> None:
    # Every field the spec documents must be an accepted ingest turn key, so an
    # operator copying the example verbatim produces a turn the pipeline reads.
    spec = standard_format_spec()
    for field in spec["fields"]:
        assert field["key"] in TURN_KEYS
        assert field["desc"]


@pytest.mark.skipif(not _REAL_TRACE_FILE.exists(), reason="sample trace file absent")
def test_second_real_export_shape_also_maps() -> None:
    # 2.json is a larger real export with the same {trace, observations} shape.
    second = Path("/home/zhangpengkun/traces/2.json")
    if not second.exists():
        pytest.skip("2.json absent")
    payload = json.loads(second.read_text(encoding="utf-8"))
    res = run_mapper_preview(default_mapper_code(), payload)
    assert res["ok"] is True
    assert res["observation_count"] >= 1
    # Prompt/response text and token metrics come through without error.
    assert isinstance(res["turn"]["metrics"]["total_tokens"], int)


# --------------------------------------------------------------------------- #
# MapperRegistry: routing, hooks, fail-open                                    #
# --------------------------------------------------------------------------- #
from teamEvolver.integrations.langfuse_mapper import (  # noqa: E402
    MapperMatch,
    MapperRegistry,
    compile_mapper_entry,
    normalize_mapper_entries,
)
from teamEvolver.integrations.langfuse_pull import convert_session_with_registry  # noqa: E402

_MAP_A = "def map_trace(t, o):\n    return {'response_text': 'FROM-A'}"
_MAP_B = "def map_trace(t, o):\n    return {'response_text': 'FROM-B'}"
_HOOK_ZHANG = (
    "def map_session(converted, session, traces):\n"
    "    return {'user_alias': 'zhang'}"
)


def test_registry_routes_first_match_in_order() -> None:
    registry = MapperRegistry.from_entries(
        [
            {"name": "specific", "enabled": True, "code": _MAP_A,
             "match": {"trace_names": ["openclaw-*"]}},
            {"name": "fallback", "enabled": True, "code": _MAP_B, "match": {}},
        ]
    )
    assert registry.for_trace(_trace(name="openclaw-turn")).name == "specific"
    assert registry.for_trace(_trace(name="other-turn")).name == "fallback"
    turn = convert_trace_to_turn(
        _trace(name="openclaw-turn"), 1, mapper=registry
    )
    assert turn["response_text"] == "FROM-A"


def test_registry_match_and_semantics() -> None:
    match = MapperMatch.from_raw(
        {"trace_names": ["agent-*"], "tags": ["foo", "bar"],
         "session_id_patterns": ["ws_*"]}
    )
    # tags are ANY-of: one hit suffices.
    assert match.matches(_trace(name="agent-1", tags=["foo"], sessionId="ws_1"))
    assert not match.matches(_trace(name="agent-1", tags=["baz"], sessionId="ws_1"))
    assert not match.matches(_trace(name="nope", tags=["foo"], sessionId="ws_1"))
    assert not match.matches(_trace(name="agent-1", tags=["foo"], sessionId="other_1"))
    # AND across groups; empty groups are unconstrained.
    assert not MapperMatch.from_raw({"trace_names": ["agent-*"]}).matches(
        _trace(name="other")
    )
    assert MapperMatch.from_raw({}).matches(_trace(name="whatever"))
    # session_id may arrive under either key.
    assert match.matches(_trace(name="agent-1", tags=["bar"], session_id="ws_9"))


def test_registry_catch_all_and_no_match() -> None:
    registry = MapperRegistry.from_entries(
        [
            {"name": "narrow", "enabled": True, "code": _MAP_A,
             "match": {"trace_names": ["never-*"]}},
        ]
    )
    assert registry.for_trace(_trace()) is None
    # map_trace falls back to the built-in defaults when nothing matches.
    builtin = convert_trace_to_turn(_trace(), 1)
    assert registry.map_trace(_trace(), [], 1, builtin) == builtin


def test_registry_skips_disabled_and_broken_entries() -> None:
    registry = MapperRegistry.from_entries(
        [
            {"name": "off", "enabled": False, "code": _MAP_A, "match": {}},
            {"name": "bad", "enabled": True, "code": "def nope(: bad", "match": {}},
            {"name": "good", "enabled": True, "code": _MAP_B, "match": {}},
        ]
    )
    assert [name for name, _ in registry.broken] == ["bad"]
    # Disabled and broken entries never route; the later entry wins.
    assert registry.for_trace(_trace()).name == "good"


def test_registry_hook_only_entry_falls_through_for_trace_mapping() -> None:
    registry = MapperRegistry.from_entries(
        [
            {"name": "hook", "enabled": True, "code": _HOOK_ZHANG, "match": {}},
            {"name": "mapper", "enabled": True, "code": _MAP_A, "match": {}},
        ]
    )
    # Hook-only entries are transparent for trace mapping...
    assert registry.for_trace(_trace()).name == "mapper"
    # ...but their session hooks still apply.
    converted = convert_session_with_registry(
        {"id": "s1"}, [_trace()], registry
    )
    assert converted["user_alias"] == "zhang"
    assert converted["turns"][0]["response_text"] == "FROM-A"


def test_registry_per_trace_fail_open() -> None:
    boom = "def map_trace(t, o):\n    raise ValueError('boom')"
    registry = MapperRegistry.from_entries(
        [{"name": "a", "enabled": True, "code": boom, "match": {"tags": ["a"]}},
         {"name": "b", "enabled": True, "code": _MAP_B, "match": {"tags": ["b"]}}]
    )
    turn_a = convert_trace_to_turn(_trace(tags=["a"]), 1, mapper=registry)
    turn_b = convert_trace_to_turn(_trace(tags=["b"]), 1, mapper=registry)
    # The raising entry degrades to the built-in turn for its trace...
    assert turn_a["response_text"] == "已执行完成"
    # ...while other traces keep their custom mapping.
    assert turn_b["response_text"] == "FROM-B"


def test_apply_session_hooks_deep_merges_and_tolerates_errors() -> None:
    registry = MapperRegistry.from_entries(
        [
            {"name": "h1", "enabled": True,
             "code": "def map_session(c, s, t):\n    raise RuntimeError('x')",
             "match": {}},
            {"name": "h2", "enabled": True,
             "code": (
                 "def map_session(c, s, t):\n"
                 "    return {'user_alias': 'u1', 'metrics': {'total_tokens': 9}}"
             ),
             "match": {}},
            {"name": "h3", "enabled": True,
             "code": "def map_session(c, s, t):\n    return 'not-a-dict'",
             "match": {}},
        ]
    )
    out = registry.apply_session_hooks(
        {"user_alias": "", "metrics": {"total_tokens": 0}}, {"id": "s1"}, [_trace()]
    )
    assert out["user_alias"] == "u1"
    assert out["metrics"]["total_tokens"] == 9


def test_normalize_mapper_entries_migration_and_shapes() -> None:
    # Key never written + legacy code -> synthesized default entry.
    migrated = normalize_mapper_entries(
        None, legacy_enabled=True, legacy_code="def map_trace(t, o): pass"
    )
    assert len(migrated) == 1
    assert migrated[0]["name"] == "default"
    assert migrated[0]["enabled"] is True
    assert migrated[0]["match"] == {"trace_names": [], "tags": [], "session_id_patterns": []}
    # Never written + disabled/empty legacy -> nothing.
    assert normalize_mapper_entries(None) == []
    assert normalize_mapper_entries(None, legacy_enabled=True, legacy_code="  ") == []
    # Explicit empty list never triggers migration.
    assert normalize_mapper_entries([], legacy_enabled=True, legacy_code="def map_trace(t,o): pass") == []
    # Raw list: name fallback, comma-string match coercion, non-dict dropped.
    entries = normalize_mapper_entries(
        [
            "junk",
            {"code": _MAP_A, "match": {"trace_names": "a-1, b-2"}},
        ]
    )
    assert len(entries) == 1
    assert entries[0]["name"] == "entry-2"
    assert entries[0]["enabled"] is True
    assert entries[0]["match"]["trace_names"] == ["a-1", "b-2"]


def test_build_mapper_registry_registry_and_legacy_fallback() -> None:
    registry = build_mapper_registry(
        TeamEvolverConfig(langfuse_mappers=[{"name": "x", "enabled": True, "code": _MAP_A}])
    )
    assert isinstance(registry, MapperRegistry)
    assert registry.for_trace(_trace()).name == "x"
    # Plain objects with only legacy fields behave as one catch-all entry.
    from types import SimpleNamespace

    legacy = SimpleNamespace(langfuse_mapper_enabled=True, langfuse_mapper_code=_MAP_A)
    legacy_registry = build_mapper_registry(legacy)
    assert legacy_registry is not None
    assert legacy_registry.for_trace(_trace()).name == "default"


def test_compile_mapper_entry_shapes() -> None:
    trace_fn, session_fn = compile_mapper_entry(_MAP_A)
    assert callable(trace_fn) and session_fn is None
    trace_fn, session_fn = compile_mapper_entry(_HOOK_ZHANG)
    assert trace_fn is None and callable(session_fn)
    trace_fn, session_fn = compile_mapper_entry(
        _MAP_A + "\n" + _HOOK_ZHANG
    )
    assert callable(trace_fn) and callable(session_fn)
    with pytest.raises(MapperError):
        compile_mapper_entry("x = 1")
    with pytest.raises(MapperError):
        compile_mapper_entry("map_session = 42")


def test_run_mapper_preview_reports_match() -> None:
    payload = {"trace": _trace(name="openclaw-turn", tags=["openclaw"])}
    res = run_mapper_preview(
        _MAP_A, payload, match={"trace_names": ["openclaw-turn"]}
    )
    assert res["ok"] is True
    assert res["match"]["matched"] is True
    assert res["match"]["unmet"] == []
    res = run_mapper_preview(
        _MAP_A, payload, match={"tags": ["other"]}
    )
    assert res["match"]["matched"] is False
    assert res["match"]["unmet"]


# --------------------------------------------------------------------------- #
# Pull pipeline with the registry (per-agent routing end-to-end)               #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_pull_sessions_routes_per_agent_with_registry(monkeypatch) -> None:
    """Two different agent_ids route to their respective adapter hooks."""
    from teamEvolver.integrations.source_adapter import AgentHooks

    config = TeamEvolverConfig(
        langfuse_enabled=True,
        langfuse_max_sessions=10,
    )

    class _PullFakeClient:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def list_session_ids(self, filters, *, max_sessions=0):
            return ["agent:main:user:111_aaa", "plain-session"]

        def fetch_session_with_traces(self, session_id, trace_name=""):
            return (
                {"id": session_id},
                [_trace(id="t1", input="work", sessionId=session_id)],
            )

    monkeypatch.setattr(langfuse_pull, "LangfuseClient", _PullFakeClient)
    monkeypatch.setattr(source_adapter, "LangfuseClient", _PullFakeClient)

    # Simulate two adapter files: one for agent-a (overrides map_trace + map_session),
    # one for agent-b (overrides map_trace only).
    _hooks_a = AgentHooks(
        map_trace=lambda trace, obs, turn_num=0, defaults=None: {"response_text": "FROM-A"},
        map_session=lambda converted, session, traces=None: {"user_alias": "zhang"},
        defined={"map_trace", "map_session"},
    )
    _hooks_b = AgentHooks(
        map_trace=lambda trace, obs, turn_num=0, defaults=None: {"response_text": "FROM-B"},
        defined={"map_trace"},
    )
    _hook_map = {"agent-a": _hooks_a, "agent-b": _hooks_b}

    monkeypatch.setattr(
        source_adapter,
        "resolve_hooks",
        lambda agent_id, config=None: _hook_map.get(agent_id, AgentHooks.all_defaults()),
    )

    ingested_a: list[dict[str, Any]] = []
    ingested_b: list[dict[str, Any]] = []

    async def _ingest_a(session: dict[str, Any]) -> dict[str, Any]:
        ingested_a.append(session)
        return {"status": "queued", "queued": True}

    async def _ingest_b(session: dict[str, Any]) -> dict[str, Any]:
        ingested_b.append(session)
        return {"status": "queued", "queued": True}

    # Pull with agent-a.
    await langfuse_pull.pull_sessions(
        config, _ingest_a, {}, max_sessions=10, agent_id="agent-a"
    )
    # Pull with agent-b.
    await langfuse_pull.pull_sessions(
        config, _ingest_b, {}, max_sessions=10, agent_id="agent-b"
    )

    # Each pull applied its own adapter's map_trace and map_session.
    by_sid_a = {s["session_id"]: s for s in ingested_a}
    by_sid_b = {s["session_id"]: s for s in ingested_b}
    # agent-a pull: sessions get FROM-A + user_alias "zhang".
    assert by_sid_a["agent-main-user-111_aaa"]["turns"][0]["response_text"] == "FROM-A"
    assert by_sid_a["agent-main-user-111_aaa"]["user_alias"] == "zhang"
    # agent-b pull: sessions get FROM-B (no session hook).
    assert by_sid_b["plain-session"]["turns"][0]["response_text"] == "FROM-B"


def test_convert_session_with_registry_fails_open() -> None:
    session = {"id": "s1"}
    traces = [_trace()]
    # None registry -> pure built-in conversion.
    builtin = convert_session_with_registry(session, traces, None)
    assert builtin["turns"][0]["response_text"] == "已执行完成"
    # A raising registry hook keeps the converted session (hook skipped).
    broken = MapperRegistry.from_entries(
        [{"name": "bad", "enabled": True, "code": "def nope(: bad", "match": {}}]
    )
    out = convert_session_with_registry(session, traces, broken)
    assert out["turns"][0]["response_text"] == "已执行完成"
