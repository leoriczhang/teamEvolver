"""会话 Meta 信息提取的公共工具。

各 agent adapter 通过 ``extract_meta(converted, session, traces)`` 钩子
（由用户在 per-agent 文件中实现）返回三个展示字段：

    user_id    —— 业务用户标识（工号 / sender 等，非平台内部 id）
    session_id —— 会话标识（缺省取标准 session_id）
    trace_id   —— 用户自定义的链路标识（藏在 trace.metadata / input 里的
                  traceId / trace_id / traceID 等，非平台自增 trace 主键）

本模块只提供两类工具，不含任何具体业务解析：
  - :func:`find_meta_value`：在 dict/list 嵌套结构里按候选 key 深度优先找值；
  - :func:`normalize_meta`：把任意 dict 收敛成仅含三个规范字段的干净 meta。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# 前端展示的规范字段（顺序即列顺序），其它键一律不透传。
META_FIELDS = ("user_id", "session_id", "trace_id")

# 递归搜索时跳过的容器 key：这些分支里的 id 是平台技术标识（如观测主键），
# 不应被当成“用户自定义 trace id”。调用方通常只把 metadata/input/output
# 传入，天然不会命中 trace 根上的平台主键。
_SKIP_KEYS = frozenset(
    {
        "observations",
        "scores",
        "projectId",
        "project_id",
        "parentObservationId",
        "session_id",
        "sessionId",
    }
)

_MAX_DEPTH = 6


def _as_scalar(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def find_meta_value(
    obj: Any,
    keys: Iterable[str],
    *,
    skip: Iterable[str] = (),
    _depth: int = 0,
) -> str:
    """在嵌套 dict/list 中按 ``keys``（候选命名，大小写不敏感）深度优先查找。

    返回第一个非空标量值（str）；找不到返回 ""。``skip`` 中的 key 对应分支
    整体跳过，避免误取平台主键。
    """
    if _depth > _MAX_DEPTH:
        return ""
    wanted = {str(k).lower() for k in keys}
    skipped = set(_SKIP_KEYS) | {str(k) for k in skip}

    def walk(node: Any, depth: int) -> Optional[str]:
        if depth > _MAX_DEPTH:
            return None
        if isinstance(node, dict):
            # 当前层先匹配，保证最近的命名优先于深层嵌套。
            for k, v in node.items():
                if str(k).lower() in wanted:
                    scalar = _as_scalar(v)
                    if scalar:
                        return scalar
            for k, v in node.items():
                if k in skipped:
                    continue
                found = walk(v, depth + 1)
                if found:
                    return found
        elif isinstance(node, (list, tuple)):
            for item in node:
                found = walk(item, depth + 1)
                if found:
                    return found
        return None

    return walk(obj, 0) or ""


def normalize_meta(raw: Any) -> dict[str, str]:
    """把 extract_meta 的返回收敛成仅含规范字段的非空字符串 dict。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for field in META_FIELDS:
        value = _as_scalar(raw.get(field))
        if value:
            out[field] = value[:200]
    return out


# 用户标识 / 自定义链路 id 的常见命名（大小写不敏感）。
DEFAULT_USER_ID_KEYS = (
    "userId", "user_id", "userID", "sender", "sender_id",
    "empId", "emp_id", "staffId", "user",
)
DEFAULT_TRACE_ID_KEYS = (
    "customTraceId", "custom_trace_id", "bizTraceId", "biz_trace_id",
    "businessTraceId", "externalTraceId", "requestId", "request_id",
    "traceId", "trace_id", "traceID",
)


def _trace_of(item: Any) -> Optional[dict[str, Any]]:
    if isinstance(item, dict):
        nested = item.get("trace")
        if isinstance(nested, dict):
            return nested
        if "input" in item or "sessionId" in item:
            return item
    return None


def collect_session_meta(
    converted: dict[str, Any],
    session: dict[str, Any],
    traces: list[Any],
    *,
    user_from_input: Optional[Any] = None,
    user_id_keys: Iterable[str] = DEFAULT_USER_ID_KEYS,
    trace_id_keys: Iterable[str] = DEFAULT_TRACE_ID_KEYS,
) -> dict[str, str]:
    """通用 meta 提取：遍历 traces，逐个补齐 user_id / session_id / trace_id。

    适配 doris.py 的 ``traces`` 形态（``[{"trace": {...}}, ...]``，亦兼容裸
    trace dict）。优先级：

    user_id:    converted.user_alias → user_from_input(input) → input 深搜 →
                session.user_id
    session_id: 首个 trace.sessionId → converted.session_id → session.id
    trace_id:   trace.metadata 深搜 → trace.input 深搜（均按 trace_id_keys）

    ``user_from_input`` 为各 agent 已有的业务解析函数（如 openclaw 的
    sender 提取）；不给则退化为按 key 深搜。
    """
    user_id = str((converted or {}).get("user_alias") or "").strip()
    session_id = ""
    trace_id = ""

    for item in traces or []:
        trace = _trace_of(item)
        if trace is None:
            continue
        if not session_id:
            session_id = str(trace.get("sessionId") or trace.get("session_id") or "").strip()
        tin = trace.get("input")
        if not user_id:
            if callable(user_from_input):
                try:
                    user_id = str(user_from_input(tin) or "").strip()
                except Exception:  # noqa: BLE001 — 用户函数异常不阻断 meta
                    user_id = ""
            if not user_id:
                user_id = find_meta_value(tin, user_id_keys)
        if not trace_id:
            meta = trace.get("metadata")
            trace_id = find_meta_value(meta, trace_id_keys) if isinstance(meta, dict) else ""
            if not trace_id:
                trace_id = find_meta_value(tin, trace_id_keys)
        if user_id and session_id and trace_id:
            break

    if not session_id:
        session_id = str(
            (converted or {}).get("session_id")
            or (session or {}).get("id")
            or ""
        ).strip()
    if not user_id:
        user_id = str((session or {}).get("user_id") or "").strip()

    return {"user_id": user_id, "session_id": session_id, "trace_id": trace_id}
