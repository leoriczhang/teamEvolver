"""Langfuse direct REST-API source, contract-aligned with DorisSourceAdapter.

连接配置（options 或环境变量，风格与 Doris 的 DORIS_* 一致）：
  host / public_key / secret_key / trace_name
  环境变量：LANGFUSE_PULL_HOST / LANGFUSE_PULL_PUBLIC_KEY / LANGFUSE_PULL_SECRET_KEY
  （与 self-tracing 用的 LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY 刻意区分：拉取源 ≠ 上报目标）
Langfuse 的 Basic 认证密钥是项目级的：默认 env 三元组只覆盖一个项目，
其余项目可通过 adapter 文件的 build_adapter 传入 options 覆盖（密钥读 env，不入 adapter 文件）。

注意：修改本文件后需重启服务——_runtime 的热重载只覆盖租户 adapter 文件，不覆盖本模块。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

from session_ingestion.adapters._shared.langfuse_client import (
    LangfuseClient,
    LangfuseError,
    SessionFilters,
)
from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session
from session_ingestion.adapters._shared.langfuse_mapper import _deep_merge_turn

logger = logging.getLogger(__name__)

_PREVIEW_TITLE_CHARS = 80


def _input_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("content", value.get("text", ""))
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                content = item.get("content", item.get("text", ""))
                parts.append(str(content or "") if not isinstance(content, list) else " ".join(
                    str(c) for c in content if not isinstance(c, dict)
                ))
            else:
                parts.append(str(item or ""))
        value = " ".join(parts)
    return str(value or "")


def _preview_title(trace: dict[str, Any]) -> str:
    in_fence = False
    for line in _input_text(trace.get("input")).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("Conversation info"):
            continue
        return stripped[:_PREVIEW_TITLE_CHARS]
    return ""


class LangfuseSource:
    source_type = "langfuse"

    def __init__(
        self,
        config,
        options: Optional[dict[str, Any]] = None,
        mapper: Optional[Callable] = None,
        session_mapper: Optional[Callable] = None,
        meta_mapper: Optional[Callable] = None,
    ):
        options = options or {}
        self.config = config
        self.host = str(
            options.get("host") or os.getenv("LANGFUSE_PULL_HOST", "") or ""
        ).strip()
        self.public_key = str(
            options.get("public_key") or os.getenv("LANGFUSE_PULL_PUBLIC_KEY", "") or ""
        ).strip()
        self.secret_key = str(
            options.get("secret_key") or os.getenv("LANGFUSE_PULL_SECRET_KEY", "") or ""
        ).strip()
        self.trace_name = str(options.get("trace_name") or "").strip()
        self.mapper = mapper
        self.session_mapper = session_mapper
        # extract_meta(converted, session, traces) -> {user_id, session_id, trace_id}
        # 由 per-agent adapter 实现，供"运行总览"会话列表展示。
        self.meta_mapper = meta_mapper
        self._local = threading.local()
        self._clients = []
        self._lock = threading.Lock()
        self._preview: dict[str, dict[str, Any]] = {}

    def _configured(self) -> bool:
        return bool(self.host and self.public_key and self.secret_key)

    def _client(self) -> LangfuseClient:
        if not self._configured():
            raise LangfuseError(
                "Langfuse direct pull is not configured. Set "
                "LANGFUSE_PULL_HOST / LANGFUSE_PULL_PUBLIC_KEY / "
                "LANGFUSE_PULL_SECRET_KEY (or pass them as adapter options)."
            )
        if not hasattr(self._local, "client"):
            self._local.client = LangfuseClient(
                self.host, self.public_key, self.secret_key,
            )
            with self._lock:
                self._clients.append(self._local.client)
        return self._local.client

    def health(self) -> dict[str, Any]:
        if not self._configured():
            return {
                "ok": False,
                "error": (
                    "Langfuse direct pull is not configured. Set "
                    "LANGFUSE_PULL_HOST / LANGFUSE_PULL_PUBLIC_KEY / "
                    "LANGFUSE_PULL_SECRET_KEY (or pass them as adapter options)."
                ),
            }
        try:
            return self._client().health()
        except Exception as exc:  # noqa: BLE001 — probe must report, not raise
            return {"ok": False, "error": str(exc)}

    def list_session_ids(self, filters, *, max_sessions):
        return self._client().list_session_ids(
            SessionFilters(**{**filters, "trace_name": self.trace_name}),
            max_sessions=max_sessions,
        )

    def fetch_session(self, session_id):
        # Doris 同构信封 {"trace": ..., "observations": [...]}：让 per-agent 的
        # map_session / extract_meta 钩子在 Doris 与 Langfuse 两种 provider 间原样复用。
        session, flat = self._client().fetch_session_with_traces(
            session_id, trace_name=self.trace_name,
        )
        traces = [
            {
                "trace": {k: v for k, v in t.items() if k != "observations"},
                "observations": t.get("observations") or [],
            }
            for t in flat
            if isinstance(t, dict)
        ]
        return session, traces

    def preview_sessions(self, filters, *, max_sessions):
        if filters.get("session_id"):
            return [{"session_id": str(filters["session_id"])}]
        trace_cap = (max_sessions * 20) if max_sessions else 0
        traces = self._client().iter_traces(
            name=self.trace_name,
            user_id=str(filters.get("user_id") or ""),
            from_timestamp=str(filters.get("from_timestamp") or ""),
            to_timestamp=str(filters.get("to_timestamp") or ""),
            order_by="timestamp.desc",
            max_items=trace_cap,
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            sid = str(trace.get("sessionId") or "").strip()
            if not sid:
                continue
            groups.setdefault(sid, []).append(trace)
        rows = []
        for sid, group in groups.items():
            first = group[0]
            rows.append({
                "session_id": sid,
                "timestamp": max(
                    (str(t.get("timestamp") or "") for t in group), default="",
                ),
                "user_id": str(first.get("userId") or ""),
                "title": _preview_title(first) or str(first.get("name") or ""),
                "trace_count": len(group),
            })
        rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
        self._preview = {row["session_id"]: row for row in rows}
        return rows[:max_sessions] if max_sessions else rows

    def convert_session(self, session, traces):
        """将 Langfuse 直连拉取的 trace 数据转为 teamEvolver 标准 session 格式。

        与 DorisSourceAdapter.convert_session 同构：信封展平后复用
        langfuse_convert（Langfuse REST 与 Doris 表结构同构），再依次应用
        session_mapper / meta_mapper 钩子。
        """
        def mapped(trace, observations, turn_num, defaults):
            patch = self.mapper(trace, observations, turn_num, defaults) if self.mapper else None
            return _deep_merge_turn(defaults, patch) if patch else defaults

        flattened = [{**item["trace"], "observations": item.get("observations") or []} for item in traces]
        converted = convert_langfuse_session(session, flattened, mapper=mapped)
        converted["source"] = "langfuse"
        if self.session_mapper:
            patch = self.session_mapper(converted, session, traces)
            if isinstance(patch, dict):
                converted.update(patch)
        if self.meta_mapper:
            try:
                from session_ingestion.adapters._shared.session_meta import normalize_meta

                meta = normalize_meta(self.meta_mapper(converted, session, traces))
                if meta:
                    converted["meta"] = meta
            except Exception as exc:  # noqa: BLE001 — meta 仅用于展示，失败不影响接入
                logger.warning("[LangfuseAdapter] extract_meta failed; ignoring: %s", exc)
        return converted

    @property
    def retry_count(self) -> int:
        with self._lock:
            return sum(client.retry_count for client in self._clients)

    def close(self):
        for client in self._clients:
            client.close()
        self._clients.clear()
