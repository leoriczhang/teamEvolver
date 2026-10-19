"""Example of adding a source-specific prompt field through a mapping hook."""

from session_ingestion.adapters._shared.trace_mapping import extract_meta, map_session, map_trace
from session_ingestion.adapters.sources.langfuse import LangfuseSource

SOURCE = {
    "label": "Example custom mapping", "provider": "langfuse",
    "host": "langfuse.example.com",
    "enabled": False, "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def custom_trace_mapping(trace, observations, turn_num=0, defaults=None):
    patch = map_trace(trace, observations, turn_num, defaults) or {}
    payload = trace.get("input")
    if isinstance(payload, dict) and isinstance(payload.get("task_description"), str):
        patch["prompt_text"] = payload["task_description"]
    return patch or None


def build_adapter():
    return LangfuseSource(
        None, {"trace_name": "agent-call"}, mapper=custom_trace_mapping,
        session_mapper=map_session, meta_mapper=extract_meta,
    )
