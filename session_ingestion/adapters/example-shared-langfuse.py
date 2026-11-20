"""One Langfuse project shared by explicitly selected business Agents."""

from session_ingestion.adapters._shared.builders import build_langfuse_adapter

SOURCE = {
    "label": "Shared business Agents / Langfuse", "provider": "langfuse",
    "host": "https://langfuse.example.com", "enabled": False, "max_sessions": 100,
    "trace_names": ["customer-agent-turn", "task-judge-turn"],
    "connection_options": ["trace_names"],
    "connection_profiles": {
        "default": {},
        "project_a": {"options": {"host": "https://langfuse.example.com"}, "env_refs": {
            "public_key": "LANGFUSE_PROJECT_A_PUBLIC_KEY", "secret_key": "LANGFUSE_PROJECT_A_SECRET_KEY"}},
        "project_b": {"options": {"host": "https://langfuse.example.com"}, "env_refs": {
            "public_key": "LANGFUSE_PROJECT_B_PUBLIC_KEY", "secret_key": "LANGFUSE_PROJECT_B_SECRET_KEY"}},
    },
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id", "agent_id",
                          "tags", "environment", "release", "version", "metadata"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter(config=None):
    return build_langfuse_adapter(SOURCE, options=config)
