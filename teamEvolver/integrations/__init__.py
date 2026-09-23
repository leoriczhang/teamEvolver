"""Integrations with external agent runtimes (Hermes, Langfuse)."""

from __future__ import annotations

from .hermes import (
    configure_hermes,
    inspect_hermes_config,
    restore_hermes_config,
)
from .langfuse_client import LangfuseClient, LangfuseError, SessionFilters
from .langfuse_mapper import (
    MapperError,
    MapperMatch,
    MapperRegistry,
    TraceMapper,
    build_mapper_registry,
    build_trace_mapper_from_config,
    compile_mapper,
    compile_mapper_entry,
    default_mapper_code,
    normalize_mapper_entries,
    run_mapper_preview,
    sample_trace_payload,
    standard_format_spec,
)
from .langfuse_pull import (
    build_filters_from_config,
    convert_session_with_registry,
    preview_sessions,
    pull_sessions,
)

__all__ = [
    "configure_hermes",
    "inspect_hermes_config",
    "restore_hermes_config",
    "LangfuseClient",
    "LangfuseError",
    "SessionFilters",
    "MapperError",
    "MapperMatch",
    "MapperRegistry",
    "TraceMapper",
    "build_mapper_registry",
    "build_trace_mapper_from_config",
    "compile_mapper",
    "compile_mapper_entry",
    "default_mapper_code",
    "normalize_mapper_entries",
    "run_mapper_preview",
    "sample_trace_payload",
    "standard_format_spec",
    "build_filters_from_config",
    "convert_session_with_registry",
    "preview_sessions",
    "pull_sessions",
]
