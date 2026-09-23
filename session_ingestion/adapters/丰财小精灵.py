# Adapter: 丰财小精灵
# Generated from sf_sessions/agents/丰财小精灵.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": '丰财小精灵',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmt9gfiff002lva06my7tcv9w',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = 'ClaudeAgent.01556ac654ed4115,ClaudeAgent.3852d3743a9e4162,ClaudeAgent.bd03e0210a8842ce,ClaudeAgent.8beacbb830714b2a'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
