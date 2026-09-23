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
import collections
import datetime as _datetime
import fnmatch
import functools
import inspect
import itertools
import json
import logging
import math
import re
from dataclasses import dataclass
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
        "collections": collections,
        "itertools": itertools,
        "functools": functools,
    }


def _exec_mapper_code(code: str, *, max_chars: int) -> dict[str, Any]:
    """Compile+exec operator ``code`` in the restricted namespace.

    Raises :class:`MapperError` on empty/oversized code, syntax errors, or any
    load-time failure of the module body (top-level helpers/constants run here
    so the entry points can reference them).
    """
    text = str(code or "").strip()
    if not text:
        raise MapperError("mapper code is empty")
    if len(text) > max_chars:
        raise MapperError(
            f"mapper code exceeds {max_chars} characters"
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
    return namespace


def compile_mapper_entry(
    code: str, *, max_chars: int = _MAX_MAPPER_CODE_CHARS
) -> tuple[Optional[Callable[..., Any]], Optional[Callable[..., Any]]]:
    """Compile one registry entry's code into ``(trace_fn, session_fn)``.

    The code block may define ``map_trace``/``map_turn`` (per-trace mapping)
    and/or ``map_session`` (post-conversion session hook). Either entry point
    may be omitted, but at least one must be present and callable. A present
    but non-callable ``map_session`` is an error.
    """
    namespace = _exec_mapper_code(code, max_chars=max_chars)

    trace_fn: Optional[Callable[..., Any]] = None
    for name in _ENTRY_NAMES:
        candidate = namespace.get(name)
        if callable(candidate):
            trace_fn = candidate
            break

    session_fn = namespace.get("map_session")
    if session_fn is not None and not callable(session_fn):
        raise MapperError("map_session must be a callable function")

    if trace_fn is None and session_fn is None:
        raise MapperError(
            "mapper code must define a top-level function named "
            f"{' or '.join(_ENTRY_NAMES)} and/or map_session"
        )
    return trace_fn, session_fn


def compile_mapper(code: str, *, max_chars: int = _MAX_MAPPER_CODE_CHARS) -> Callable[..., Any]:
    """Compile operator ``code`` and return its ``map_trace``/``map_turn`` callable.

    Raises :class:`MapperError` on syntax errors, a missing entry point, or a
    non-callable entry point.
    """
    trace_fn, _ = compile_mapper_entry(code, max_chars=max_chars)
    if trace_fn is None:
        raise MapperError(
            "mapper code must define a top-level function named "
            f"{' or '.join(_ENTRY_NAMES)}"
        )
    return trace_fn


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


# --------------------------------------------------------------------------- #
# Per-agent mapper registry                                                    #
# --------------------------------------------------------------------------- #

_MATCH_KEYS = ("trace_names", "tags", "session_id_patterns")


def _as_pattern_list(raw: Any) -> list[str]:
    """Coerce a match value (list or comma-separated string) into a clean list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [raw]
    out: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def normalize_match(raw: Any) -> dict[str, list[str]]:
    """Normalize one entry's ``match`` block into the three canonical lists.

    Accepts list/tuple/set or comma-separated strings per key; empty values are
    dropped and duplicates removed. An empty result means catch-all.
    """
    match = raw if isinstance(raw, dict) else {}
    return {key: _as_pattern_list(match.get(key)) for key in _MATCH_KEYS}


def normalize_mapper_entries(
    raw: Any, *, legacy_enabled: bool = False, legacy_code: str = ""
) -> list[dict[str, Any]]:
    """Normalize the ``langfuse.mappers`` registry into canonical entry dicts.

    Each entry becomes ``{name, enabled, note, code, match}`` with ``match``
    normalized by :func:`normalize_match`.

    Migration (idempotent): when ``raw is None`` — i.e. the key was never
    written — and ``legacy_code`` is non-empty, a single catch-all entry named
    ``default`` is synthesized from the legacy single-mapper fields. An
    explicit empty list or a list never triggers migration, so a deliberate
    ``mappers: []`` disables the legacy mapper permanently.
    """
    if raw is None:
        code = str(legacy_code or "").strip()
        if not code:
            return []
        return [
            {
                "name": "default",
                "enabled": bool(legacy_enabled),
                "note": "由旧版单 mapper 配置迁移而来",
                "code": str(legacy_code),
                "match": normalize_match(None),
            }
        ]
    if not isinstance(raw, list):
        logger.warning(
            "[Langfuse] ignoring non-list mappers config: %s", type(raw).__name__
        )
        return []
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            logger.warning(
                "[Langfuse] dropping non-dict mapper entry at index %d", index
            )
            continue
        name = str(item.get("name") or "").strip() or f"entry-{index + 1}"
        entries.append(
            {
                "name": name,
                "enabled": bool(item.get("enabled", True)),
                "note": str(item.get("note") or ""),
                "code": str(item.get("code") or ""),
                "match": normalize_match(item.get("match")),
            }
        )
    return entries


@dataclass(frozen=True)
class MapperMatch:
    """Routing constraints for one registry entry (AND of non-empty groups).

    - ``trace_names``: fnmatch patterns (case-sensitive) against ``trace.name``.
    - ``tags``: ANY-of — the trace carries at least one of these tags.
    - ``session_id_patterns``: fnmatch patterns against the trace's sessionId
      (prefix patterns are expressed as ``prefix*``).

    All groups empty = catch-all.
    """

    trace_names: tuple[str, ...] = ()
    tags: frozenset = frozenset()
    session_id_patterns: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: Any) -> "MapperMatch":
        normalized = normalize_match(raw)
        return cls(
            trace_names=tuple(normalized["trace_names"]),
            tags=frozenset(normalized["tags"]),
            session_id_patterns=tuple(normalized["session_id_patterns"]),
        )

    @staticmethod
    def _session_id(trace: dict[str, Any]) -> str:
        return str(trace.get("sessionId") or trace.get("session_id") or "")

    def matches(self, trace: dict[str, Any]) -> bool:
        if not isinstance(trace, dict):
            return False
        if self.trace_names:
            name = str(trace.get("name") or "")
            if not any(fnmatch.fnmatchcase(name, p) for p in self.trace_names):
                return False
        if self.tags:
            trace_tags = {str(t) for t in trace.get("tags") or []}
            if not (self.tags & trace_tags):
                return False
        if self.session_id_patterns:
            sid = self._session_id(trace)
            if not any(fnmatch.fnmatchcase(sid, p) for p in self.session_id_patterns):
                return False
        return True

    def unmet_reasons(self, trace: dict[str, Any]) -> list[str]:
        """Human-readable reasons each constraint group failed (for previews)."""
        if not isinstance(trace, dict):
            return ["trace 不是对象"]
        reasons: list[str] = []
        if self.trace_names:
            name = str(trace.get("name") or "")
            if not any(fnmatch.fnmatchcase(name, p) for p in self.trace_names):
                reasons.append(
                    f"trace name {name!r} 未命中 {list(self.trace_names)}"
                )
        if self.tags:
            trace_tags = {str(t) for t in trace.get("tags") or []}
            if not (self.tags & trace_tags):
                reasons.append(
                    f"trace tags {sorted(trace_tags)} 未包含 "
                    f"{sorted(self.tags)} 中任一标签"
                )
        if self.session_id_patterns:
            sid = self._session_id(trace)
            if not any(fnmatch.fnmatchcase(sid, p) for p in self.session_id_patterns):
                reasons.append(
                    f"sessionId {sid!r} 未命中 {list(self.session_id_patterns)}"
                )
        return reasons

    def describe(self) -> str:
        parts: list[str] = []
        if self.trace_names:
            parts.append("name~" + "|".join(self.trace_names))
        if self.tags:
            parts.append("tag:" + "|".join(sorted(self.tags)))
        if self.session_id_patterns:
            parts.append("sid~" + "|".join(self.session_id_patterns))
        return " · ".join(parts) if parts else "全部匹配"


@dataclass
class CompiledMapperEntry:
    """One registry entry with its compiled entry points.

    ``trace_mapper`` is None for hook-only entries (code defines only
    ``map_session``) and for entries whose code failed to compile (they never
    match; see :attr:`MapperRegistry.broken`).
    """

    name: str
    index: int
    enabled: bool
    note: str
    match: MapperMatch
    trace_mapper: Optional[TraceMapper] = None
    session_fn: Optional[Callable[..., Any]] = None


def _invoke_session_hook(
    fn: Callable[..., Any],
    converted: dict[str, Any],
    session: dict[str, Any],
    traces: list[dict[str, Any]],
) -> Any:
    """Call ``map_session(converted, session, traces)`` adapting to its arity.

    ``*args`` gets all three positionally; otherwise positional params are
    filled from the left. Keyword-only params fall back to caller error
    handling (the hook is skipped with a warning).
    """
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(converted, session, traces)
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return fn(converted, session, traces)
    count = sum(
        1
        for p in parameters
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    args = (converted, session, traces)
    return fn(*args[:count])


class MapperRegistry:
    """Ordered per-agent mapper registry; the first matching entry wins.

    Callable with the mapper protocol of ``convert_trace_to_turn``
    (``registry(trace, observations, turn_num, defaults)``), so it can be
    passed directly as the ``mapper`` argument.
    """

    def __init__(
        self,
        entries: list[CompiledMapperEntry],
        broken: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.entries = entries
        self.broken = list(broken or [])

    @classmethod
    def from_entries(cls, raw: Any) -> "MapperRegistry":
        """Build a registry from raw config entries (normalized internally).

        Enabled entries whose code fails to compile are recorded in
        ``self.broken`` with a warning and can never match — routing falls to
        later entries or the built-in mapping (fail-open, consistent with the
        single-mapper behavior).
        """
        normalized = normalize_mapper_entries(raw)
        entries: list[CompiledMapperEntry] = []
        broken: list[tuple[str, str]] = []
        for index, entry in enumerate(normalized):
            name = entry["name"]
            trace_mapper: Optional[TraceMapper] = None
            session_fn: Optional[Callable[..., Any]] = None
            if entry["enabled"]:
                try:
                    trace_fn, hook_fn = compile_mapper_entry(entry["code"])
                    if trace_fn is not None:
                        trace_mapper = TraceMapper(trace_fn, source=str(entry["code"]))
                    session_fn = hook_fn
                except MapperError as exc:
                    broken.append((name, str(exc)))
                    logger.warning(
                        "[Langfuse] mapper entry %r disabled (compile failed): %s",
                        name,
                        exc,
                    )
            entries.append(
                CompiledMapperEntry(
                    name=name,
                    index=index,
                    enabled=bool(entry["enabled"]),
                    note=entry["note"],
                    match=MapperMatch.from_raw(entry["match"]),
                    trace_mapper=trace_mapper,
                    session_fn=session_fn,
                )
            )
        return cls(entries, broken)

    def for_trace(self, trace: dict[str, Any]) -> Optional[CompiledMapperEntry]:
        """First matching enabled entry that carries a trace mapper.

        Hook-only entries are transparent for trace mapping (they still apply
        their session hooks); no match → None (built-in mapping).
        """
        for entry in self.entries:
            if (
                entry.enabled
                and entry.trace_mapper is not None
                and entry.match.matches(trace)
            ):
                return entry
        return None

    def map_trace(
        self,
        trace: dict[str, Any],
        observations: list[dict[str, Any]],
        turn_num: int,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        """Per-trace mapping with fail-open: errors fall back to ``defaults``."""
        entry = self.for_trace(trace)
        if entry is None or entry.trace_mapper is None:
            return defaults
        try:
            return entry.trace_mapper(trace, observations, turn_num, defaults)
        except Exception as exc:  # noqa: BLE001 - operator code can raise anything
            logger.warning(
                "[Langfuse] mapper entry %r failed on trace %s; using built-in mapping: %s",
                entry.name,
                trace.get("id") if isinstance(trace, dict) else "?",
                exc,
            )
            return defaults

    __call__ = map_trace

    def apply_session_hooks(
        self,
        converted: dict[str, Any],
        session: dict[str, Any],
        traces: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run matched entries' ``map_session`` hooks, deep-merging in order.

        A hook runs iff its entry is enabled, defines ``map_session``, and
        matches at least one trace of the session. Errors and non-dict returns
        are skipped with a warning; the converted session is preserved.
        """
        result = converted if isinstance(converted, dict) else {}
        trace_dicts = [t for t in traces or [] if isinstance(t, dict)]
        for entry in self.entries:
            if not entry.enabled or entry.session_fn is None:
                continue
            if not any(entry.match.matches(t) for t in trace_dicts):
                continue
            try:
                raw = _invoke_session_hook(entry.session_fn, result, session, traces)
            except Exception as exc:  # noqa: BLE001 - operator code can raise anything
                logger.warning(
                    "[Langfuse] mapper entry %r map_session failed; hook skipped: %s",
                    entry.name,
                    exc,
                )
                continue
            if raw is None:
                continue
            if not isinstance(raw, dict):
                logger.warning(
                    "[Langfuse] mapper entry %r map_session must return a dict "
                    "or None, got %s; hook skipped",
                    entry.name,
                    type(raw).__name__,
                )
                continue
            result = _deep_merge_turn(result, _jsonable(raw))
        return result

    def route_report(self, trace: dict[str, Any]) -> dict[str, Any]:
        """Explain routing for one trace (used by the console route preview)."""
        per_entry: list[dict[str, Any]] = []
        matched: Optional[dict[str, Any]] = None
        for entry in self.entries:
            did_match = bool(entry.enabled and entry.match.matches(trace))
            per_entry.append(
                {
                    "index": entry.index,
                    "name": entry.name,
                    "enabled": entry.enabled,
                    "matched": did_match,
                }
            )
            if matched is None and did_match and entry.trace_mapper is not None:
                matched = {"index": entry.index, "name": entry.name}
        return {
            "order": [entry.name for entry in self.entries],
            "matched": matched,
            "per_entry": per_entry,
        }


def build_mapper_registry(config: Any) -> Optional[MapperRegistry]:
    """Build the per-agent mapper registry from config (None → built-in only).

    Reads ``config.langfuse_mappers`` (list). When empty, falls back to the
    legacy single-mapper fields as one catch-all entry — this covers config
    objects not built by :mod:`teamEvolver.config_store.bridge` (e.g. the
    local pull script's SimpleNamespace) and keeps old tests/configs working.
    """
    entries = getattr(config, "langfuse_mappers", None)
    if entries:
        return MapperRegistry.from_entries(entries)
    if bool(getattr(config, "langfuse_mapper_enabled", False)):
        code = str(getattr(config, "langfuse_mapper_code", "") or "")
        if code.strip():
            return MapperRegistry.from_entries(
                [{"name": "default", "enabled": True, "code": code, "match": {}}]
            )
    return None


def build_trace_mapper_from_config(config: Any) -> Optional[MapperRegistry]:
    """Deprecated alias for :func:`build_mapper_registry`.

    The returned registry is callable with the mapper protocol used by
    ``convert_trace_to_turn``, so existing call sites keep working unchanged.
    """
    return build_mapper_registry(config)


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
    match: Any = None,
) -> dict[str, Any]:
    """Dry-run ``code`` against a pasted trace and return a structured result.

    Returns ``{"ok": True, "turn": <mapped turn>, "builtin": <built-in turn>}``
    on success or ``{"ok": False, "error": "..."}`` on any failure. Never
    raises, so the console tester can render either branch directly.

    When ``match`` is provided (an entry's ``match`` block), the result also
    carries ``{"match": {"matched": bool, "unmet": [str, ...]}}`` so the
    console can show whether this trace would route to the entry.
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
        trace_fn, _ = compile_mapper_entry(code)
    except MapperError as exc:
        return {"ok": False, "error": str(exc)}

    if trace_fn is None:
        # Hook-only entry (map_session without map_trace): no per-turn mapping.
        return {
            "ok": True,
            "turn": _jsonable(builtin_turn),
            "builtin": _jsonable(builtin_turn),
            "observation_count": len(observations),
            "note": "该条目仅定义 map_session（会话钩子），无轮级映射",
        }

    mapper = TraceMapper(trace_fn, source=str(code))
    try:
        turn = convert_trace_to_turn(trace, turn_num, mapper=mapper)
    except MapperError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - operator code can raise anything
        return {"ok": False, "error": f"mapper raised {type(exc).__name__}: {exc}"}

    result = {
        "ok": True,
        "turn": _jsonable(turn),
        "builtin": _jsonable(builtin_turn),
        "observation_count": len(observations),
    }
    if match is not None:
        matcher = MapperMatch.from_raw(match)
        result["match"] = {
            "matched": matcher.matches(trace),
            "unmet": matcher.unmet_reasons(trace),
        }
    return result


def _validate_mapper_registry_entries(raw: Any) -> list[dict[str, Any]]:
    """Validate + normalize a legacy registry during offline migration.

    Raises ``ValueError`` with an operator-facing message on the first problem:
    entry names must be non-empty and unique, and enabled entries need code
    that compiles (``map_trace``/``map_turn`` and/or ``map_session``).
    """
    if not isinstance(raw, list):
        raise ValueError("mappers 必须是列表")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"mappers[{index}] 必须是对象")
        if not str(item.get("name") or "").strip():
            raise ValueError(f"mappers[{index}] 缺少 name")
    entries = normalize_mapper_entries(raw)
    seen: set[str] = set()
    for entry in entries:
        name = entry["name"]
        if name in seen:
            raise ValueError(f"mapper 名称重复: {name!r}")
        seen.add(name)
        if entry["enabled"]:
            if not entry["code"].strip():
                raise ValueError(f"mapper {name!r} 无法启用：代码为空")
            try:
                compile_mapper_entry(entry["code"])
            except MapperError as exc:
                raise ValueError(f"mapper {name!r} 无法启用：{exc}") from exc
    return entries
