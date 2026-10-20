"""Source constructors using the shared trace mapping."""
from __future__ import annotations

from typing import Any

from .trace_mapping import extract_meta, map_session, map_trace


def build_doris_adapter(
    source: dict[str, Any],
    trace_name: str,
):
    from session_ingestion.adapters.sources.doris import DorisSourceAdapter

    return DorisSourceAdapter(
        None,
        {"project_id": source["project_id"], "trace_name": trace_name},
        mapper=map_trace,
        session_mapper=map_session,
        meta_mapper=extract_meta,
    )


def build_langfuse_adapter(
    source: dict[str, Any],
    trace_name: str,
    options: dict[str, Any] | None = None,
):
    """Langfuse 直连版 build_doris_adapter：字段映射完全一致，仅换传输层。

    连接默认走 LANGFUSE_PULL_HOST/PUBLIC_KEY/SECRET_KEY 环境变量（一套默认
    三元组只覆盖一个 Langfuse 项目）；其他项目在租户 adapter 文件里通过
    ``options`` 覆盖（值读 os.environ，密钥不写入 adapter 文件）。
    """
    from session_ingestion.adapters.sources.langfuse import LangfuseSource

    return LangfuseSource(
        None,
        {"trace_name": trace_name, **(options or {})},
        mapper=map_trace,
        session_mapper=map_session,
        meta_mapper=extract_meta,
    )
