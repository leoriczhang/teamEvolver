"""Apache Doris source template; publish and bind it before pulling Sessions."""

from session_ingestion.adapters._shared.builders import build_doris_adapter

SOURCE = {
    "label": "Example Doris", "provider": "doris",
    "host": "doris.example.com", "project_id": "example-project",
    "enabled": False, "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    return build_doris_adapter(SOURCE, "agent-call")
