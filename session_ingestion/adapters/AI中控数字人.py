# Adapter: AI中控数字人
# Generated from sf_sessions/agents/AI中控数字人.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": 'AI中控数字人',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmqj2pt2x02ghxa06h29yv7qo',
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
