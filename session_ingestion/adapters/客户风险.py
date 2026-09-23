# Adapter: 客户风险
# Generated from sf_sessions/agents/客户风险.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": '客户风险',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmr33byjl038fxa06vh9pseik',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = '/aiAgent/customerRiskInfo,/aiAgent/customerFileIdRiskInfo,/aiAgent/riskKnowledgeInfo,/aiAgent/queryWeeklyRiskAnswer,/disposer/aiAgent/customerFileIdRiskInfo,/disposer/aiAgent/queryWeeklyRiskAnswer,/disposer/aiAgent/customerRiskInfo'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
