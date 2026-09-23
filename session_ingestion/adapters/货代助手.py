# Adapter: 货代助手
# Generated from sf_sessions/agents/货代助手.json and agents/_summary.json.
# Field mapping is centralized in adapters/_shared/sf_agent_adapter.py.

SOURCE = {
    "label": '货代助手',
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": 'cmqjemdfc02ioxa060k0b2q31',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}

TRACE_NAME = 'intent_identification,inquiryAndQuotation,knowledge,general,solution,fba_export,fba_transfer_number,transport_selector,fba_amazon_fist,dashboard,fba_customs_merge,fba_sales_assistant,waybill_fee_correction'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_doris_adapter,
    )

    return build_sf_doris_adapter(SOURCE, TRACE_NAME)
