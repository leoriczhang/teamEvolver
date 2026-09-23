# Adapter: AI客服
# Generated from sf_sessions/agents/AI客服.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": 'AI客服',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmqul3q2p02tkxa06tji8rnr2',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = 'LangGraph'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
