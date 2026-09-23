# Adapter: 场地规划
# Generated from sf_sessions/agents/场地规划.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": '场地规划',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmq7zz90p01wqxa068vayea7l',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = 'POST /intent/dingtalk/stream'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
