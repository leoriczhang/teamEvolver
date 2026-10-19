"""Langfuse source template using LANGFUSE_PULL_* environment credentials."""

from session_ingestion.adapters._shared.builders import build_langfuse_adapter

SOURCE = {
    "label": "Example Langfuse", "provider": "langfuse",
    "host": "langfuse.example.com",
    "enabled": False, "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    return build_langfuse_adapter(SOURCE, "agent-call")
