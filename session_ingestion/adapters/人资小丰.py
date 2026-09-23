# Adapter: 人资小丰
# Generated from sf_sessions/agents/人资小丰.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": '人资小丰',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cms8lvaa000efug0618j06rnp',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = 'agent-call'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
