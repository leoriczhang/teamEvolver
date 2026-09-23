# Adapter: 产品设计（Langfuse 直连）
# 与 产品设计.py（Doris 版）并存：字段映射一致，仅传输层从 Doris SQL 换为
# Langfuse REST API。切换数据源 = 控制台重新绑定 adapter 文件，无需改代码。
# 连接配置走环境变量（风格同 DORIS_*）：
#   LANGFUSE_PULL_HOST / LANGFUSE_PULL_PUBLIC_KEY / LANGFUSE_PULL_SECRET_KEY
#   （密钥为项目级，实际以当前加载的 .env 为准；本地 .env 目前指向生产
#   ai-langfuse.sf-express.com，SOURCE.host 仅供页面展示，不参与请求）
# 注意：修改 sources/langfuse.py 或 _shared/sf_agent_adapter.py 后需重启服务
# （热重载仅覆盖本目录的租户 adapter 文件）。
# Session id 噪音在公共 pull runtime 中按 SOURCE 配置前置过滤。

SOURCE = {
    "label": '产品设计',
    "provider": "langfuse",
    "host": "https://ai-langfuse.sf-express.com",
    "project_id": 'cmov8jh43015yxa06g4nu4xfd',
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
    "exclude_session_id_patterns": ["origin-*", "rollout-*"],
}

TRACE_NAME = 'openclaw-turn'


def build_adapter():
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        build_sf_langfuse_adapter,
    )

    return build_sf_langfuse_adapter(SOURCE, TRACE_NAME)
