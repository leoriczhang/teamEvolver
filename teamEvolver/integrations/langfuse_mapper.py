"""User-authorable mapping from a Langfuse trace + observations to the
teamEvolver "standard evolution format" (one interaction turn).

Langfuse traces and observations share one flat shape; observations only add
nesting through ``parentObservationId``. The mapping into the evolution turn is
otherwise mechanical, so instead of hardcoding one interpretation we let an
operator paste a small Python function and own that mapping:

    def map_trace(trace, observations):
        # trace:        dict — one Langfuse trace (input/output/metadata/...)
        # observations: list[dict] — its observations, flat but nested via
        #               parentObservationId (GENERATION / SPAN / EVENT / ...)
        # return:       dict — a teamEvolver turn (see TURN_KEYS). Partial dicts
        #               are deep-merged over the built-in mapping, so a function
        #               only needs to override the fields it cares about.
        #               Return None to accept the built-in mapping as-is.
        ...

The function runs in-process with a restricted builtin set and the ``json`` /
``re`` / ``math`` / ``datetime`` modules pre-injected (``import`` is disabled).
This is **admin-authored, trusted** configuration — the guardrails stop casual
mistakes (``open``/``exec``/``__import__``), not a determined operator. Only
admins can set the code (enforced at the ``/api/langfuse-config`` route).

The module is intentionally pure (no network, no config object) so the compile,
merge, and preview logic can be unit-tested with fixture payloads. Callers that
want the configured mapper use :func:`build_trace_mapper_from_config`.
"""

from __future__ import annotations

import builtins
import datetime as _datetime
import json
import logging
import math
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The turn keys the built-in converter produces and the ingest contract reads.
# Exposed so the console can show operators the target shape.
TURN_KEYS = (
    "turn_num",
    "trace_id",
    "prompt_text",
    "response_text",
    "messages",
    "tool_calls",
    "tool_results",
    "injected_skills",
    "used_skills",
    "read_skills",
    "modified_skills",
    "metrics",
    "_langfuse",
)

# Canonical + accepted-alias names for the mapping entry point.
_ENTRY_NAMES = ("map_trace", "map_turn")

# Builtins we expose to mapper code. Deliberately excludes filesystem/eval/import
# primitives (open, exec, eval, compile, __import__, input, globals, locals,
# vars, exit, quit, help, breakpoint, setattr, delattr).
_ALLOWED_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "getattr", "hasattr", "hash", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "ord", "chr", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    "True", "False", "None",
    # Exceptions a mapper may reasonably raise/catch.
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError",
)

_MAX_MAPPER_CODE_CHARS = 20_000


class MapperError(ValueError):
    """Raised when mapper code fails to compile, load, or execute."""


def _safe_builtins() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _ALLOWED_BUILTIN_NAMES:
        value = getattr(builtins, name, None)
        if value is not None or name == "None":
            out[name] = value
    return out


def _mapper_globals() -> dict[str, Any]:
    """Namespace exposed to mapper code: safe builtins + a few stdlib modules."""
    return {
        "__builtins__": _safe_builtins(),
        "json": json,
        "re": re,
        "math": math,
        "datetime": _datetime,
    }


def compile_mapper(code: str) -> Callable[..., Any]:
    """Compile operator ``code`` and return its ``map_trace``/``map_turn`` callable.

    Raises :class:`MapperError` on syntax errors, a missing entry point, or a
    non-callable entry point. Execution of the module body happens here (so
    top-level helpers/constants are available to the entry point), but the
    trace-mapping call itself is deferred to :class:`TraceMapper`.
    """
    text = str(code or "").strip()
    if not text:
        raise MapperError("mapper code is empty")
    if len(text) > _MAX_MAPPER_CODE_CHARS:
        raise MapperError(
            f"mapper code exceeds {_MAX_MAPPER_CODE_CHARS} characters"
        )
    try:
        compiled = compile(text, "<langfuse_mapper>", "exec")
    except SyntaxError as exc:
        raise MapperError(f"syntax error: {exc}") from exc

    namespace = _mapper_globals()
    try:
        exec(compiled, namespace)  # noqa: S102 - trusted admin config, restricted builtins
    except Exception as exc:  # noqa: BLE001 - surface any load-time failure
        raise MapperError(f"failed to load mapper: {type(exc).__name__}: {exc}") from exc

    for name in _ENTRY_NAMES:
        candidate = namespace.get(name)
        if callable(candidate):
            return candidate
    raise MapperError(
        "mapper code must define a top-level function named "
        f"{' or '.join(_ENTRY_NAMES)}"
    )


def _deep_merge_turn(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``; nested dicts merge, everything else wins.

    Lists and scalars from ``override`` replace the base value wholesale so an
    operator can, e.g., return ``{"tool_calls": [...]}`` to replace the built-in
    list, while ``{"metrics": {"tool_call_count": 3}}`` only patches one metric.
    """
    result = dict(base)
    for key, value in override.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _deep_merge_turn(result[key], value)
        else:
            result[key] = value
    return result


def _jsonable(value: Any, *, _depth: int = 0) -> Any:
    """Coerce mapper output into JSON-safe primitives, defensively bounded."""
    if _depth > 12:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, _depth=_depth + 1) for v in value]
    return str(value)


class TraceMapper:
    """Callable wrapper around compiled operator code.

    Instances are used exactly like :func:`convert_trace_to_turn`'s optional
    ``mapper`` argument: ``mapper(trace, observations, turn_num, defaults)``.
    The wrapper adapts to whatever positional/keyword parameters the operator's
    function declares, validates the result, and deep-merges partial results
    over the built-in ``defaults`` turn.
    """

    def __init__(self, fn: Callable[..., Any], *, source: str = "") -> None:
        self._fn = fn
        self._source = source

    @classmethod
    def from_code(cls, code: str) -> "TraceMapper":
        return cls(compile_mapper(code), source=str(code or ""))

    def _invoke(
        self,
        trace: dict[str, Any],
        observations: list[dict[str, Any]],
        turn_num: int,
        defaults: dict[str, Any],
    ) -> Any:
        """Call the operator fn, adapting to whatever signature it declares.

        Supported shapes, in priority order:
          - ``**kwargs`` present  -> call with the four known kwargs by name;
          - all params are known names (any order) -> bind those by name;
          - otherwise (positional/unknown names)   -> pass positionally in the
            canonical order (trace, observations, turn_num, defaults), truncated
            to the declared arity (``*args`` gets all four).
        """
        import inspect

        try:
            parameters = inspect.signature(self._fn).parameters
        except (TypeError, ValueError):
            # Builtins / C-callables expose no signature: pass positionally.
            return self._fn(trace, observations, turn_num, defaults)

        available = {
            "trace": trace,
            "observations": observations,
            "turn_num": turn_num,
            "defaults": defaults,
        }
        ordered = [trace, observations, turn_num, defaults]

        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
        accepts_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters.values()
        )
        # Named parameters the operator declared (excludes *args/**kwargs).
        declared = [
            name
            for name, p in parameters.items()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_ONLY,
            )
        ]

        if accepts_varargs:
            return self._fn(*ordered)
        if accepts_kwargs:
            # Positional-only params (rare) must go by position; everything else
            # the operator declared plus the full known set flows in as kwargs so
            # a bare ``def map_trace(**kw)`` still sees trace/observations/etc.
            positional_only = [
                available[name]
                for name, p in parameters.items()
                if p.kind == inspect.Parameter.POSITIONAL_ONLY and name in available
            ]
            kwargs = {
                name: value
                for name, value in available.items()
                if name not in {
                    n
                    for n, p in parameters.items()
                    if p.kind == inspect.Parameter.POSITIONAL_ONLY
                }
            }
            return self._fn(*positional_only, **kwargs)
        if declared and all(n in available for n in declared):
            return self._fn(**{n: available[n] for n in declared})
        # Positional / unknown parameter names: pass by canonical position.
        return self._fn(*ordered[: len(declared)])

    def __call__(
        self,
        trace: dict[str, Any],
        observations: list[dict[str, Any]],
        turn_num: int,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        raw = self._invoke(trace, observations, turn_num, defaults)
        if raw is None:
            return defaults
        if not isinstance(raw, dict):
            raise MapperError(
                f"{_ENTRY_NAMES[0]} must return a dict or None, got {type(raw).__name__}"
            )
        merged = _deep_merge_turn(defaults, _jsonable(raw))
        # turn_num is authoritative from the pipeline ordering; keep it stable
        # unless the operator deliberately set a positive integer.
        try:
            supplied = int(raw.get("turn_num")) if "turn_num" in raw else 0
        except (TypeError, ValueError):
            supplied = 0
        merged["turn_num"] = supplied if supplied > 0 else turn_num
        return merged


def build_trace_mapper_from_config(config: Any) -> Optional[TraceMapper]:
    """Return a :class:`TraceMapper` when the operator enabled a custom mapper.

    Returns ``None`` when the feature is disabled or the code is empty. Compile
    errors are logged and swallowed (returns ``None``) so a broken mapper never
    blocks a pull — the caller falls back to the built-in converter.
    """
    if not bool(getattr(config, "langfuse_mapper_enabled", False)):
        return None
    code = str(getattr(config, "langfuse_mapper_code", "") or "").strip()
    if not code:
        return None
    try:
        return TraceMapper.from_code(code)
    except MapperError as exc:
        logger.warning("[Langfuse] custom trace mapper disabled (compile failed): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Reference template + bundled sample (used by the console dry-run tester)     #
# --------------------------------------------------------------------------- #

def standard_format_spec() -> dict[str, Any]:
    """Describe the "standard evolution format" a mapper must return.

    Returned to the console so the operator can see, next to their editor,
    exactly which fields the evolution pipeline consumes, what each means, and a
    filled example. Kept here (beside :data:`TURN_KEYS` and the ingest contract)
    so the doc never drifts from the code.

    A mapper returns one *turn* (one Langfuse trace == one interaction turn). A
    partial turn is deep-merged over the built-in mapping, so only overridden
    fields need to be present; ``prompt_text`` **or** ``response_text`` must end
    up non-empty for the turn to be ingested.
    """
    fields = [
        {
            "key": "turn_num",
            "type": "int",
            "required": False,
            "desc": "轮次序号（从 1 开始）。留空时由拉取顺序自动分配。",
        },
        {
            "key": "trace_id",
            "type": "str",
            "required": False,
            "desc": "来源 Langfuse trace 的 id，便于回溯。留空时取 trace.id。",
        },
        {
            "key": "prompt_text",
            "type": "str",
            "required": "至少其一",
            "desc": "本轮用户/输入侧文本。与 response_text 至少要有一个非空，否则该会话按空内容跳过。",
        },
        {
            "key": "response_text",
            "type": "str",
            "required": "至少其一",
            "desc": "本轮 Agent/输出侧文本。",
        },
        {
            "key": "messages",
            "type": "list[dict]",
            "required": False,
            "desc": "完整消息序列，每条形如 {role, content, tool_calls?}。用于进化时还原对话。",
        },
        {
            "key": "tool_calls",
            "type": "list[dict]",
            "required": False,
            "desc": "工具调用，形如 {id, type, function:{name, arguments}}。arguments 为字符串化 JSON。",
        },
        {
            "key": "tool_results",
            "type": "list[dict]",
            "required": False,
            "desc": "工具返回，形如 {tool_call_id, tool_name, content, has_error}。",
        },
        {
            "key": "injected_skills",
            "type": "list[str]",
            "required": False,
            "desc": "本轮注入到上下文的团队 Skill 名称。",
        },
        {
            "key": "used_skills",
            "type": "list[str]",
            "required": False,
            "desc": "本轮实际使用（命中）的 Skill 名称。",
        },
        {
            "key": "read_skills",
            "type": "list[dict]",
            "required": False,
            "desc": "本轮读取过的 Skill，元素形如 {skill_name}。",
        },
        {
            "key": "modified_skills",
            "type": "list[dict]",
            "required": False,
            "desc": "本轮被创建/修改的 Skill，元素形如 {skill_name}。",
        },
        {
            "key": "metrics",
            "type": "dict",
            "required": False,
            "desc": (
                "效率指标：tool_call_count / api_call_count / input_tokens / "
                "output_tokens / total_tokens。会在会话级自动汇总（轮次优先策略的核心口径）。"
            ),
        },
        {
            "key": "_langfuse",
            "type": "dict",
            "required": False,
            "desc": "来源侧元数据（trace_name / tags / models / environment / timestamp 等），供审计与检索。",
        },
    ]
    example = {
        "turn_num": 1,
        "trace_id": "ddda7e6b-0dc8-4752-819e-2b546196f4b3",
        "prompt_text": "请运行本地脚本并静默完成任务。",
        "response_text": "脚本已执行，跳过发送（今日已发送过）。",
        "messages": [
            {"role": "user", "content": "请运行本地脚本…"},
            {"role": "assistant", "content": "脚本已执行…"},
        ],
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "exec", "arguments": "{\"command\": \"python3 report.py\"}"},
            }
        ],
        "tool_results": [
            {"tool_call_id": "call_1", "tool_name": "exec", "content": "SKIP:already sent today", "has_error": False}
        ],
        "injected_skills": [],
        "used_skills": [],
        "read_skills": [],
        "modified_skills": [],
        "metrics": {
            "tool_call_count": 1,
            "api_call_count": 2,
            "input_tokens": 533,
            "output_tokens": 87,
            "total_tokens": 3180,
        },
        "_langfuse": {
            "trace_name": "openclaw-turn",
            "tags": ["main", "openclaw"],
            "models": ["glm-5.2"],
            "environment": "default",
            "timestamp": "2026-07-01T01:39:10.237000+00:00",
        },
    }
    return {
        "title": "进化标准格式（Evolution Turn）",
        "summary": (
            "一个 Langfuse trace 对应一个交互轮次（turn）。map_trace 返回该 turn 字典；"
            "返回部分字段会深合并到内置映射之上，返回 None 表示完全使用内置映射。"
            "prompt_text 与 response_text 至少要有一个非空。"
        ),
        "fields": fields,
        "example": example,
    }


def default_mapper_code() -> str:
    """A runnable reference mapper operators can start editing from.

    It reproduces a subset of the built-in mapping (prompt/response + token and
    tool-call metrics) using only ``trace`` and ``observations`` so the shape of
    a real mapping is obvious.
    """
    return '''# map_trace(trace, observations) -> teamEvolver evolution turn (dict).
# Return a partial dict to override only some fields; the rest fall back to
# teamEvolver's built-in mapping. Return None to accept the built-in mapping.
#
# Available: json, re, math, datetime. `import` is disabled.

def map_trace(trace, observations):
    meta = trace.get("metadata") or {}
    usage = meta.get("usage") or {}

    # Count tool activity from tool/exec spans; sum tokens from GENERATIONs.
    tool_calls = 0
    generations = 0
    for obs in observations or []:
        obs_type = str(obs.get("type") or "").upper()
        name = str(obs.get("name") or "").lower()
        if obs_type == "GENERATION":
            generations += 1
        elif name.startswith("tool:"):
            tool_calls += 1

    return {
        "prompt_text": str(trace.get("input") or ""),
        "response_text": str(trace.get("output") or ""),
        "metrics": {
            "tool_call_count": tool_calls,
            "api_call_count": generations,
            "input_tokens": int(usage.get("input") or 0),
            "output_tokens": int(usage.get("output") or 0),
            "total_tokens": int(usage.get("total") or 0),
        },
        "_langfuse": {
            "trace_name": str(trace.get("name") or ""),
            "success": bool(meta.get("success", True)),
        },
    }
'''


def sample_trace_payload() -> dict[str, Any]:
    """A minimal ``{trace, observations}`` fixture for the dry-run tester.

    Mirrors the real Langfuse export shape (one trace + a flat, parent-linked
    observation list) so operators can test a mapper without a live pull.
    """
    trace = {
        "id": "ddda7e6b-0dc8-4752-819e-2b546196f4b3",
        "name": "openclaw-turn",
        "timestamp": "2026-07-01T01:39:10.237000+00:00",
        "sessionId": "agent:main:cron:3989221a",
        "tags": ["main", "openclaw"],
        "input": "请运行本地脚本并静默完成任务。",
        "output": "脚本已执行，跳过发送（今日已发送过）。",
        "metadata": {
            "success": True,
            "usage": {"input": 533, "output": 87, "total": 3180, "unit": "TOKENS"},
            "llmCallCount": 2,
        },
    }
    observations = [
        {
            "id": "715e75ba-329f-4574-bfc4-9de17b3f3ddc",
            "traceId": trace["id"],
            "type": "GENERATION",
            "name": "LLM Request (loop 1)",
            "parentObservationId": "8606b1c0-36cc-404d-a2c2-697190f1fb2f",
            "model": "ep-20260402171724-w7dcc",
            "usage": {"input": 362, "output": 72, "total": 1586, "unit": "TOKENS"},
        },
        {
            "id": "23430d46-b61f-4d09-b474-626949ef616f",
            "traceId": trace["id"],
            "type": "SPAN",
            "name": "tool: exec",
            "parentObservationId": "8606b1c0-36cc-404d-a2c2-697190f1fb2f",
            "input": {"command": "python3 report.py", "timeout": 300},
            "output": {"content": [{"type": "text", "text": "SKIP:already sent today"}]},
        },
        {
            "id": "8606b1c0-36cc-404d-a2c2-697190f1fb2f",
            "traceId": trace["id"],
            "type": "SPAN",
            "name": "agent loop 1",
            "parentObservationId": None,
        },
    ]
    return {"trace": trace, "observations": observations}


def normalize_trace_input(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split arbitrary pasted JSON into ``(trace, observations)``.

    Accepts either ``{"trace": {...}, "observations": [...]}`` (the export shape
    in ``/home/zhangpengkun/traces/*.json``) or a bare trace dict that already
    embeds ``observations``. Raises :class:`MapperError` for anything else.
    """
    if not isinstance(payload, dict):
        raise MapperError("trace payload must be a JSON object")
    if isinstance(payload.get("trace"), dict):
        trace = dict(payload["trace"])
        observations = payload.get("observations")
        if observations is None:
            observations = trace.get("observations")
    else:
        trace = dict(payload)
        observations = trace.get("observations")
    if observations is None:
        observations = []
    if not isinstance(observations, list):
        raise MapperError("observations must be a JSON array")
    obs_list = [o for o in observations if isinstance(o, dict)]
    # Keep observations reachable from within the trace too, so a mapper written
    # against ``trace["observations"]`` behaves the same as one using the arg.
    trace.setdefault("observations", obs_list)
    return trace, obs_list


def run_mapper_preview(
    code: str,
    payload: Any,
    *,
    turn_num: int = 1,
) -> dict[str, Any]:
    """Dry-run ``code`` against a pasted trace and return a structured result.

    Returns ``{"ok": True, "turn": <mapped turn>, "builtin": <built-in turn>}``
    on success or ``{"ok": False, "error": "..."}`` on any failure. Never
    raises, so the console tester can render either branch directly.
    """
    # Imported lazily to avoid a circular import (convert imports nothing here,
    # but preview needs the built-in baseline turn to show the merge result).
    from .langfuse_convert import convert_trace_to_turn

    try:
        trace, observations = normalize_trace_input(payload)
    except MapperError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        builtin_turn = convert_trace_to_turn(trace, turn_num)
    except Exception as exc:  # noqa: BLE001 - defensive; built-in should not raise
        return {"ok": False, "error": f"built-in conversion failed: {exc}"}

    try:
        mapper = TraceMapper.from_code(code)
    except MapperError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        turn = convert_trace_to_turn(trace, turn_num, mapper=mapper)
    except MapperError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - operator code can raise anything
        return {"ok": False, "error": f"mapper raised {type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "turn": _jsonable(turn),
        "builtin": _jsonable(builtin_turn),
        "observation_count": len(observations),
    }
