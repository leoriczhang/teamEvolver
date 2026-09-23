"""Generic data-source adapter abstraction for session ingestion.

This module decouples teamEvolver's session-pull pipeline from any
specific data source (Langfuse, Doris, custom APIs, ...). The design follows the
pattern proven in the skill-opt project:

  - **System layer** (base filtering, empty-content skip) is handled by the
    pipeline and cannot be bypassed by adapters.
  - **Data-source layer** (how to list sessions, fetch details, convert
    raw traces) is provided by a ``SourceAdapter`` implementation. Built-ins:
    ``LangfuseSourceAdapter`` (``langfuse``) and ``LegacyConverterSource``
    (``skillopt``). Any OTHER data source is a single file:
    ``session_ingestion/adapters/sources/<source_type>.py`` defining ``build_adapter(config,
    options)`` — discovered automatically from ``datasource.type`` and
    hot-reloaded by mtime (see :func:`build_file_source_adapter`).
  - **Per-agent layer** (business-specific filtering, field extraction,
    dedup, custom conversion) is an optional file in ``session_ingestion/adapters/<agent_id>.py``
    that can override individual hooks. Unchanged hooks fall back to the
    adapter defaults. Files are hot-reloaded by mtime.

Hook contract (all optional — define what you need, omit the rest)::

    def filter_session(session, ctx) -> dict | None:
        '''Return filtered session dict, or None to drop it.'''

    def extract_fields(session, ctx) -> dict:
        '''Return extra fields to merge into the session (e.g. emp_id).'''

    def dedup(sessions, ctx) -> list[dict]:
        '''Return de-duplicated session list.'''

    def map_trace(trace, observations, turn_num, defaults) -> dict | None:
        '''Return a mapped turn dict (deep-merged over defaults), or None
        to accept the built-in mapping.'''

    def map_session(converted, session, traces) -> dict | None:
        '''Post-conversion session hook; return partial dict to deep-merge,
        or None to keep as-is.'''

``ctx`` is a lightweight dataclass carrying config, filters, and utilities.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .langfuse_client import LangfuseClient

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Hook names (canonical order)                                                 #
# --------------------------------------------------------------------------- #

HOOK_NAMES = (
    "filter_session",
    "extract_fields",
    "dedup",
    "map_trace",
    "map_session",
)

# Directory for per-agent adapter files.
# Defaults to the repo-bundled session_ingestion/adapters/ directory so they ship with the code.
# Override with config ``datasource.adapters_dir`` for a custom location.
_REPO_ADAPTERS_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_ADAPTERS_DIR = _REPO_ADAPTERS_DIR


# --------------------------------------------------------------------------- #
# Adapter context                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class AdapterContext:
    """Lightweight context passed to every hook call.

    Carries everything a hook needs without depending on the full config
    object, so it is safe to use from both the web process and CLI.
    """

    agent_id: str = ""
    source_type: str = "langfuse"
    config: Any = None
    filters: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    # Lazily-populated lookup of already-processed session_ids (for dedup).
    _seen_sessions: set[str] = field(default_factory=set, repr=False)


# --------------------------------------------------------------------------- #
# SourceAdapter protocol                                                       #
# --------------------------------------------------------------------------- #


class SourceAdapter(Protocol):
    """Transport-neutral adapter for pulling sessions from a data source."""

    def list_session_ids(
        self, filters: dict[str, Any], *, max_sessions: int
    ) -> list[str]:
        """Return matching session ids from the data source."""

    def fetch_session(
        self, session_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch one session and its full traces (with observations)."""

    def convert_session(
        self,
        session: dict[str, Any],
        traces: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Convert raw session + traces into the teamEvolver session format."""

    def health(self) -> dict[str, Any]:
        """Probe connectivity; return ``{"ok": bool, ...}``."""

    @property
    def source_type(self) -> str:
        """Human-readable source type identifier (e.g. ``"langfuse"``)."""
        ...


# --------------------------------------------------------------------------- #
# Built-in Langfuse adapter                                                    #
# --------------------------------------------------------------------------- #


class LangfuseSourceAdapter:
    """Default adapter: pulls from Langfuse v3 public REST API.

    Wraps the existing :class:`LangfuseClient` so the refactored pipeline
    keeps identical behaviour. Per-agent hooks can override the conversion
    and filtering without touching this class.
    """

    source_type = "langfuse"

    def __init__(self, config: Any) -> None:
        self._config = config

    def _client(self):
        # Module-level import so tests can monkeypatch source_adapter.LangfuseClient.
        return LangfuseClient.from_config(self._config)

    def list_session_ids(
        self, filters: dict[str, Any], *, max_sessions: int
    ) -> list[str]:
        from .langfuse_client import SessionFilters

        client = self._client()
        lf_filters = SessionFilters(
            from_timestamp=str(filters.get("from_timestamp") or ""),
            to_timestamp=str(filters.get("to_timestamp") or ""),
            environment=filters.get("environment") or [],
            user_id=str(filters.get("user_id") or ""),
            tags=filters.get("tags") or [],
            release=str(filters.get("release") or ""),
            version=str(filters.get("version") or ""),
            trace_name=str(filters.get("trace_name") or ""),
            session_id=str(filters.get("session_id") or ""),
            metadata=filters.get("metadata") or {},
        )
        return client.list_session_ids(lf_filters, max_sessions=max_sessions)

    def fetch_session(
        self, session_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        client = self._client()
        trace_name = str(
            getattr(self._config, "langfuse_default_trace_name", "") or ""
        )
        return client.fetch_session_with_traces(session_id, trace_name=trace_name)

    def convert_session(
        self,
        session: dict[str, Any],
        traces: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from .langfuse_mapper import build_mapper_registry
        from .langfuse_pull import convert_session_with_registry

        return convert_session_with_registry(session, traces, build_mapper_registry(self._config))

    def health(self) -> dict[str, Any]:
        try:
            return self._client().health()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Adapter registry (source-type → adapter class)                               #
# --------------------------------------------------------------------------- #

_ADAPTER_CLASSES: dict[str, Callable[[Any], SourceAdapter]] = {
    "langfuse": LangfuseSourceAdapter,
}


def register_source_adapter(
    source_type: str, factory: Callable[[Any], SourceAdapter]
) -> None:
    """Register a custom source adapter factory.

    ``factory`` receives the config object and must return a
    :class:`SourceAdapter` instance.
    """
    _ADAPTER_CLASSES[source_type] = factory


class SourceAdapterError(RuntimeError):
    """Raised when a file-based source adapter fails to load or build.

    Deliberately fail-loud: if the admin selected a custom source type and
    its file is broken, silently falling back to Langfuse would pull from the
    wrong data source.
    """


def build_source_adapter(config: Any) -> SourceAdapter:
    """Build the adapter for the configured source type.

    Resolution order:
      1. ``"skillopt"`` → :class:`LegacyConverterSource`
      2. built-in / registered types (``_ADAPTER_CLASSES``)
      3. file-based discovery: ``<adapters_dir>/sources/<type>.py`` (mtime
         hot-reloaded; load failures raise :class:`SourceAdapterError`)
      4. unknown types fall back to langfuse with a warning so a
         misconfiguration never hard-crashes the pipeline.
    """
    source_type = str(
        getattr(config, "datasource_type", "") or "langfuse"
    ).strip().lower()
    if source_type == "skillopt":
        from .legacy_converter import LegacyConverterSource

        return LegacyConverterSource(config)
    factory = _ADAPTER_CLASSES.get(source_type)
    if factory is not None:
        return factory(config)
    file_adapter = build_file_source_adapter(source_type, config)
    if file_adapter is not None:
        return file_adapter
    logger.warning(
        "[SourceAdapter] unknown source type %r, falling back to langfuse",
        source_type,
    )
    return _ADAPTER_CLASSES["langfuse"](config)


# --------------------------------------------------------------------------- #
# File-based source adapters (<adapters_dir>/sources/<source_type>.py)        #
# --------------------------------------------------------------------------- #

_SOURCES_SUBDIR = "sources"

_source_module_cache: dict[str, tuple[float, Any]] = {}
_source_module_lock = threading.Lock()


def _sources_dir(config: Any = None) -> Path:
    """Directory for file-based source adapters (``session_ingestion/adapters/sources``)."""
    return _adapters_dir(config) / _SOURCES_SUBDIR


def source_adapter_path(source_type: str, config: Any = None) -> Optional[Path]:
    """Find the file-based adapter for ``source_type``.

    Looks for ``session_ingestion/adapters/sources/<source_type>.py``. Returns None when no
    file exists.
    """
    safe_id = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in str(source_type or "")
    ).strip("._-")
    if not safe_id:
        return None
    candidate = _sources_dir(config) / f"{safe_id}.py"
    return candidate if candidate.exists() else None


def list_file_source_types(config: Any = None) -> list[str]:
    """List available file-based source types (for console validation)."""
    directory = _sources_dir(config)
    if not directory.is_dir():
        return []
    return [
        p.stem
        for p in sorted(directory.glob("*.py"))
        if not p.name.startswith("_") and not p.name.startswith("__")
    ]


def _load_source_module(path: Path) -> Any:
    """Load a source-adapter module with mtime-based hot-reload."""
    mtime = path.stat().st_mtime
    cache_key = str(path)
    with _source_module_lock:
        cached = _source_module_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

    module_name = f"te_source_adapter_{hashlib.md5(cache_key.encode()).hexdigest()[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise SourceAdapterError(f"cannot load source adapter {path}: spec is None")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # noqa: S102 — trusted admin code
    with _source_module_lock:
        _source_module_cache[cache_key] = (mtime, module)
    return module


def _invoke_build_adapter(factory: Callable[..., Any], config: Any, options: dict) -> Any:
    """Call ``build_adapter`` adapting to its declared arity.

    ``def build_adapter(config, options)`` gets both; ``def build_adapter(config)``
    gets only the config (options remain reachable via ``config.datasource_options``);
    ``*args``/``**kwargs`` shapes get both by position/keyword.
    """
    try:
        params = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        return factory(config, options)
    kinds = {p.kind for p in params}
    positional = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    ]
    if inspect.Parameter.VAR_POSITIONAL in kinds or len(positional) >= 2:
        return factory(config, options)
    if inspect.Parameter.VAR_KEYWORD in kinds and not positional:
        return factory(config=config, options=options)
    if len(positional) == 1:
        return factory(config)
    return factory()


def build_file_source_adapter(
    source_type: str, config: Any
) -> Optional[SourceAdapter]:
    """Build a source adapter from ``session_ingestion/adapters/sources/<source_type>.py``.

    The file must define ``build_adapter(config, options)`` (options optional)
    returning an object with ``list_session_ids`` / ``fetch_session`` /
    ``convert_session``. Returns None when no file exists for the type.
    Load/build failures raise :class:`SourceAdapterError` (fail-loud).
    """
    path = source_adapter_path(source_type, config)
    if path is None:
        return None

    try:
        module = _load_source_module(path)
    except SourceAdapterError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any load-time failure
        raise SourceAdapterError(
            f"failed to load source adapter {path}: {type(exc).__name__}: {exc}"
        ) from exc

    factory = getattr(module, "build_adapter", None)
    if not callable(factory):
        raise SourceAdapterError(
            f"source adapter {path} must define a callable "
            "build_adapter(config, options)"
        )

    options = getattr(config, "datasource_options", None)
    if not isinstance(options, dict):
        options = {}
    try:
        adapter = _invoke_build_adapter(factory, config, options)
    except Exception as exc:  # noqa: BLE001 — trusted admin code can raise anything
        raise SourceAdapterError(
            f"build_adapter in {path} failed: {type(exc).__name__}: {exc}"
        ) from exc

    missing = [
        name
        for name in ("list_session_ids", "fetch_session", "convert_session")
        if not callable(getattr(adapter, name, None))
    ]
    if missing:
        raise SourceAdapterError(
            f"source adapter {path} is missing required methods: "
            + ", ".join(missing)
        )

    if not str(getattr(adapter, "source_type", "") or ""):
        try:
            adapter.source_type = str(source_type)
        except Exception:  # noqa: BLE001 — slotted/frozen class; informational only
            pass
    return adapter


def clear_source_module_cache() -> None:
    """Clear the file-based source-adapter cache (used by tests)."""
    with _source_module_lock:
        _source_module_cache.clear()


# --------------------------------------------------------------------------- #
# Per-agent hook file loader (importlib + mtime hot-reload)                    #
# --------------------------------------------------------------------------- #


@dataclass
class AgentHooks:
    """Resolved hooks for one agent: callable or None for each hook name."""

    filter_session: Optional[Callable] = None
    extract_fields: Optional[Callable] = None
    dedup: Optional[Callable] = None
    map_trace: Optional[Callable] = None
    map_session: Optional[Callable] = None
    defined: set[str] = field(default_factory=set)
    source: str = ""  # file path or "<inline>"

    @classmethod
    def from_module(cls, module: Any, path: str = "") -> "AgentHooks":
        """Build from a loaded module, taking only known hook names."""
        hooks = cls(source=path)
        for name in HOOK_NAMES:
            fn = getattr(module, name, None)
            if callable(fn):
                setattr(hooks, name, fn)
                hooks.defined.add(name)
        return hooks

    @classmethod
    def all_defaults(cls) -> "AgentHooks":
        """No overrides — all hooks fall back to system defaults."""
        return cls()


_hook_cache: dict[str, tuple[float, AgentHooks]] = {}
_hook_lock = threading.Lock()


def _adapters_dir(config: Any = None) -> Path:
    """Resolve the adapters directory from config or default."""
    explicit = str(getattr(config, "datasource_adapters_dir", "") or getattr(config, "adapters_dir", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return _DEFAULT_ADAPTERS_DIR


def adapter_path(agent_id: str, config: Any = None) -> Optional[Path]:
    """Find the adapter file for ``agent_id``.

    Looks for ``session_ingestion/adapters/<agent_id>.py``. Returns None when no file exists
    (the caller falls back to all-default hooks).
    """
    safe_id = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in str(agent_id or "")
    ).strip("._-")
    if not safe_id:
        return None
    candidate = _adapters_dir(config) / f"{safe_id}.py"
    return candidate if candidate.exists() else None


def resolve_hooks(
    agent_id: str, config: Any = None
) -> AgentHooks:
    """Load per-agent hooks with mtime-based hot-reload.

    Returns :class:`AgentHooks.all_defaults` when no file exists.
    """
    path = adapter_path(agent_id, config)
    if path is None:
        return AgentHooks.all_defaults()

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return AgentHooks.all_defaults()

    cache_key = str(path)
    with _hook_lock:
        cached = _hook_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

    # Load the module.
    module_name = f"te_adapter_{hashlib.md5(cache_key.encode()).hexdigest()[:8]}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            logger.warning("[SourceAdapter] cannot load %s: spec is None", path)
            return AgentHooks.all_defaults()
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # noqa: S102 — trusted admin code
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] failed to load adapter %s: %s", path, exc
        )
        return AgentHooks.all_defaults()

    hooks = AgentHooks.from_module(module, path=str(path))
    with _hook_lock:
        _hook_cache[cache_key] = (mtime, hooks)
    logger.debug(
        "[SourceAdapter] loaded %s (hooks: %s)", path, sorted(hooks.defined)
    )
    return hooks


def clear_hook_cache() -> None:
    """Clear the hot-reload cache (used by tests)."""
    with _hook_lock:
        _hook_cache.clear()


# --------------------------------------------------------------------------- #
# Hook execution helpers (fail-open / fail-loud)                               #
# --------------------------------------------------------------------------- #


def run_filter_session(
    hooks: AgentHooks, session: dict[str, Any], ctx: AdapterContext
) -> Optional[dict[str, Any]]:
    """Run the filter_session hook; fail-open (return session on error)."""
    if hooks.filter_session is None:
        return session
    try:
        result = hooks.filter_session(session, ctx)
        return result if result is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] filter_session failed for agent %s; keeping session: %s",
            ctx.agent_id,
            exc,
        )
        return session


def run_extract_fields(
    hooks: AgentHooks, session: dict[str, Any], ctx: AdapterContext
) -> dict[str, Any]:
    """Run the extract_fields hook; fail-open (return {} on error)."""
    if hooks.extract_fields is None:
        return {}
    try:
        result = hooks.extract_fields(session, ctx)
        return result if isinstance(result, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] extract_fields failed for agent %s; skipping: %s",
            ctx.agent_id,
            exc,
        )
        return {}


def run_dedup(
    hooks: AgentHooks,
    sessions: list[dict[str, Any]],
    ctx: AdapterContext,
) -> list[dict[str, Any]]:
    """Run the dedup hook; fail-open (return sessions unchanged on error)."""
    if hooks.dedup is None:
        return sessions
    try:
        result = hooks.dedup(sessions, ctx)
        return result if isinstance(result, list) else sessions
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] dedup failed for agent %s; skipping: %s",
            ctx.agent_id,
            exc,
        )
        return sessions


def run_map_trace(
    hooks: AgentHooks,
    trace: dict[str, Any],
    observations: list[dict[str, Any]],
    turn_num: int,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Run the map_trace hook; fail-open (return defaults on error)."""
    if hooks.map_trace is None:
        return defaults
    try:
        result = hooks.map_trace(trace, observations, turn_num, defaults)
        if result is None:
            return defaults
        if not isinstance(result, dict):
            logger.warning(
                "[SourceAdapter] map_trace must return dict or None; using defaults"
            )
            return defaults
        # Deep-merge over defaults (reuse the existing deep_merge_turn).
        from .langfuse_mapper import _deep_merge_turn, _jsonable

        return _deep_merge_turn(defaults, _jsonable(result))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] map_trace failed; using built-in mapping: %s", exc
        )
        return defaults


def run_map_session(
    hooks: AgentHooks,
    converted: dict[str, Any],
    session: dict[str, Any],
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the map_session hook; fail-open (return converted on error)."""
    if hooks.map_session is None:
        return converted
    try:
        result = hooks.map_session(converted, session, traces)
        if result is None:
            return converted
        if not isinstance(result, dict):
            return converted
        from .langfuse_mapper import _deep_merge_turn, _jsonable

        return _deep_merge_turn(converted, _jsonable(result))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SourceAdapter] map_session failed; keeping un-hooked result: %s",
            exc,
        )
        return converted


# --------------------------------------------------------------------------- #
# Default adapter template (for scaffolding)                                   #
# --------------------------------------------------------------------------- #


def default_adapter_template(agent_id: str = "") -> str:
    """Return a starter adapter file that operators can edit.

    All hooks are commented out; uncomment the ones you need.
    """
    return f'''# Adapter for agent: {agent_id or "<agent_id>"}
#
# This file is hot-reloaded (edit + save = instant effect, no restart).
# Define only the hooks you need; the rest fall back to system defaults.
# Available context fields:
#   ctx.agent_id   — this agent's id
#   ctx.source_type — "langfuse" (or custom source type)
#   ctx.config     — full config object (for advanced use)
#   ctx.filters    — pull-time filter overrides

# def filter_session(session, ctx):
#     """Return the session dict to keep it, or None to drop it."""
#     # Example: skip sessions with no tool calls
#     # turns = session.get("turns") or []
#     # if not any(t.get("tool_calls") for t in turns):
#     #     return None
#     return session

# def extract_fields(session, ctx) -> dict:
#     """Return extra fields to merge into the session."""
#     return {{"emp_id": session.get("user_alias", "")}}

# def dedup(sessions, ctx) -> list:
#     """Return de-duplicated session list."""
#     seen = set()
#     result = []
#     for s in sessions:
#         sid = s.get("session_id", "")
#         if sid not in seen:
#             seen.add(sid)
#             result.append(s)
#     return result

# def map_trace(trace, observations, turn_num, defaults):
#     """Return a partial turn dict (deep-merged over defaults), or None."""
#     return {{
#         "prompt_text": str(trace.get("input") or ""),
#         "response_text": str(trace.get("output") or ""),
#     }}

# def map_session(converted, session, traces):
#     """Post-conversion session hook; return partial dict or None."""
#     return None
'''


def default_source_adapter_template(source_type: str = "") -> str:
    """Return a starter file-based source adapter for operators to edit.

    Save as ``<adapters_dir>/sources/<source_type>.py`` and set
    ``datasource.type`` to the file name; the file is hot-reloaded by mtime.
    """
    st = source_type or "<source_type>"
    return f'''# Source adapter: {st}
#
# Save this file as  <adapters_dir>/sources/{st}.py
# and set the config  datasource.type: {st}  (connection params go in
# datasource.options, a free-form dict passed to build_adapter below).
# The file is hot-reloaded (edit + save = instant effect, no restart).
#
# Only THREE methods are required: list_session_ids / fetch_session /
# convert_session. The pipeline handles concurrency, empty-session skip,
# session_id sanitization, dedup, ingest, and evolution triggering.
# Optional: health() for the connectivity probe, close() for cleanup,
# preview_sessions(filters, max_sessions) for rich console previews.


def build_adapter(config, options):
    """config — full TeamEvolverConfig; options — datasource.options dict."""
    return {st.title().replace("-", "").replace("_", "")}Adapter(config, options)


class {st.title().replace("-", "").replace("_", "")}Adapter:
    source_type = "{st}"

    def __init__(self, config, options):
        self.config = config
        self.options = options or {{}}
        # e.g. self.host = options.get("host", "")

    def list_session_ids(self, filters, *, max_sessions):
        """Return up to max_sessions session ids matching filters.

        filters keys: from_timestamp / to_timestamp (ISO strings),
        user_id, tags, session_id, trace_name — apply what your source
        supports, ignore the rest.
        """
        raise NotImplementedError

    def fetch_session(self, session_id):
        """Fetch one session. Return (session_dict, traces_list).

        traces_list: one dict per interaction turn, in chronological order.
        Each trace dict is whatever your convert_session consumes.
        """
        raise NotImplementedError

    def convert_session(self, session, traces):
        """Convert to the teamEvolver session format. Required shape:

        {{
            "session_id": str,
            "title": str,                       # optional
            "turns": [
                {{
                    "prompt_text": str,          # user input (or response_text)
                    "response_text": str,        # agent output
                    "messages": [{{"role": ..., "content": ...}}],
                    "tool_calls": [{{"id", "type": "function",
                                    "function": {{"name", "arguments"}}}}],
                    "tool_results": [{{"tool_call_id", "tool_name",
                                      "content", "has_error"}}],
                    "injected_skills": [], "used_skills": [],
                    "metrics": {{"tool_call_count", "api_call_count",
                                "input_tokens", "output_tokens",
                                "total_tokens"}},
                }},
            ],
        }}
        A session with no non-empty prompt/response/tool activity in any turn
        is skipped as empty, so only include meaningful turns.
        """
        turns = []
        for i, trace in enumerate(traces or [], 1):
            turns.append({{
                "turn_num": i,
                "prompt_text": str(trace.get("input") or ""),
                "response_text": str(trace.get("output") or ""),
                "metrics": {{"total_tokens": 0}},
            }})
        return {{"session_id": session.get("id") or "", "turns": turns}}

    # Optional: connectivity probe for the console.
    # def health(self):
    #     return {{"ok": True}}

    # Optional: release connections / caches after a pull.
    # def close(self):
    #     pass
'''

