"""One explicit Trace selection shared by discovery, preview and detail fetches."""
from __future__ import annotations

from collections.abc import Mapping


def trace_names(single="", multiple=None):
    if not isinstance(single, str):
        raise ValueError("trace_name must be text")
    if multiple is not None and (not isinstance(multiple, list) or not 1 <= len(multiple) <= 32):
        raise ValueError("trace_names must contain 1..32 explicit Trace names")
    values = ([single] if single.strip() else []) + (multiple or [])
    if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
        raise ValueError("Trace names must be nonempty text of at most 200 characters")
    return tuple(dict.fromkeys(value.strip() for value in values))


def agent_id(trace):
    metadata = trace.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return ""
    observation = metadata.get("teamevolver_execution") or {}
    nested = observation.get("agent_id") if isinstance(observation, Mapping) else None
    flat = metadata.get("agent_id")
    if flat and nested and flat != nested:
        return ""
    return flat or nested or ""


def matches_trace(trace, filters):
    names = trace_names(filters.trace_name, filters.trace_names or None)
    if names and str(trace.get("name") or "").strip() not in names:
        return False
    if filters.agent_id and agent_id(trace) != filters.agent_id:
        return False
    if filters.user_id and trace.get("userId") != filters.user_id:
        return False
    if filters.environment and trace.get("environment") not in filters.environment:
        return False
    if filters.tags and not set(filters.tags).issubset(trace.get("tags") or []):
        return False
    if any(expected and trace.get(key) != expected
           for key, expected in (("release", filters.release), ("version", filters.version))):
        return False
    metadata = trace.get("metadata") or {}
    if filters.metadata and not isinstance(metadata, Mapping):
        return False
    for key, expected in filters.metadata.items():
        actual = metadata.get(key)
        if isinstance(expected, bool):
            if type(actual) is not bool or actual != expected:
                return False
        elif isinstance(expected, (int, float)):
            if type(actual) not in (int, float) or actual != expected:
                return False
        elif actual != str(expected):
            return False
    return True
