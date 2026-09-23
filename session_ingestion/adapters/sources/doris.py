# Source adapter: doris
#
# 从 Apache Doris 拉取 Langfuse 格式的 trace 数据（统一走 Doris，不再直连 Langfuse API）。
# Doris 表结构与 inc-aiagent-core-skill-opt/core/doris_client.py 一致：
#   - langfuse_traces_log_new_grey（trace 表）
#   - langfuse_observations_log_new_grey（observation 表）
#   - langfuse_project_id_mapping_grey（项目 ID 映射表）
#
# 每个 agent 的 project_id 在 datasource.options 或 per-agent adapter 头部注释中配置。
# Doris 帐号密码从环境变量读取（.env_prd / .env_sit）：
#   DORIS_HOSTS, DORIS_PORT, DORIS_USER, DORIS_PASSWORD, DORIS_DB
#
# 热重载：改完保存即生效，无需重启服务。

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# =====================================================================
# 连接配置（环境变量可覆盖）
# =====================================================================
DORIS_HOSTS = [
    h.strip()
    for h in os.getenv("DORIS_HOSTS", "").split(",")
    if h.strip()
]
DORIS_PORT = int(os.getenv("DORIS_PORT", "9030"))
DORIS_USER = os.getenv("DORIS_USER", "")
DORIS_PASSWORD = os.getenv("DORIS_PASSWORD", "")
DORIS_DB = os.getenv("DORIS_DB", "")
DORIS_PROXY_URL = os.getenv(
    "DORIS_PROXY_URL",
    "https://skill-opt.sf-express.com/api/doris/query",
)
_FORCE_PROXY = os.getenv("DORIS_FORCE_PROXY", "").strip() in ("1", "true", "yes")

# 表名
T_TRACES = "langfuse_traces_log_new_grey"
T_OBSERVATIONS = "langfuse_observations_log_new_grey"
T_MAPPING = "langfuse_project_id_mapping_grey"

_OBS_COLS = (
    "trace_id, id, type, name, parent_observation_id, start_time, end_time, "
    "IF(type='SPAN', input, NULL) AS input, output, level, status_message, "
    "provided_model_name, model_parameters"
)
_TRACE_COLS = "id, project_id, name, timestamp, session_id, user_id, input, output"
_SYNTHETIC_SESSION_PREFIX = "doris-trace:"

_pid_cache: dict[str, str] = {}
_pid_cache_lock = threading.Lock()
_rr_counter = itertools.count(os.getpid())


def _host_order() -> list[str]:
    n = len(DORIS_HOSTS)
    idx = next(_rr_counter) % n
    return [DORIS_HOSTS[(idx + i) % n] for i in range(n)]


def _sql_str(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("'", "''")


def _trace_name_condition(trace_name: Any) -> str:
    names = list(dict.fromkeys(name.strip() for name in str(trace_name or "").split(",") if name.strip()))
    if len(names) == 1:
        return f" AND name = '{_sql_str(names[0])}'"
    if names:
        return " AND name IN (" + ",".join(f"'{_sql_str(name)}'" for name in names) + ")"
    return ""


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in ("{", "["):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _ts_to_iso(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    s = str(v).replace(" ", "T")
    if s and "+" not in s and not s.endswith("Z"):
        s += "+00:00"
    return s


def _synthetic_session_id(trace_id: Any) -> str:
    value = str(trace_id or "").strip()
    return f"{_SYNTHETIC_SESSION_PREFIX}{value}" if value else ""


def _synthetic_trace_id(session_id: Any) -> str:
    value = str(session_id or "")
    if not value.startswith(_SYNTHETIC_SESSION_PREFIX):
        return ""
    return value[len(_SYNTHETIC_SESSION_PREFIX):].strip()


# =====================================================================
# 底层查询：直连优先，HTTP 代理降级
# =====================================================================
_direct_failed = _FORCE_PROXY


def _query_direct(sql: str, timeout: int = 120) -> Optional[list[dict]]:
    global _direct_failed
    if _direct_failed or _FORCE_PROXY:
        return None
    try:
        import pymysql
    except ImportError:
        _direct_failed = True
        return None
    last_err = None
    for host in _host_order():
        try:
            conn = pymysql.connect(
                host=host, port=DORIS_PORT, user=DORIS_USER,
                password=DORIS_PASSWORD, database=DORIS_DB,
                charset="utf8mb4", connect_timeout=5,
                read_timeout=timeout, write_timeout=timeout,
                cursorclass=pymysql.cursors.DictCursor,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    return list(cur.fetchall())
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
            inner = e if isinstance(e, socket.gaierror) else getattr(e, "__cause__", None)
            if isinstance(inner, socket.gaierror):
                break
    logger.warning(
        "[DorisAdapter] 直连失败，降级 HTTP 代理（%s: %s）",
        type(last_err).__name__ if last_err else "unknown",
        str(last_err)[:80] if last_err else "",
    )
    _direct_failed = True
    return None


def _query_proxy(sql: str, timeout: int = 180, retries: int = 3) -> list[dict]:
    import httpx
    payload = {
        "sql": sql, "host": _host_order()[0], "port": DORIS_PORT,
        "username": DORIS_USER, "password": DORIS_PASSWORD, "database": DORIS_DB,
    }
    ca_bundle = os.getenv("DORIS_CA_BUNDLE")
    verify = ca_bundle if ca_bundle else False
    last_err: Optional[str] = None
    for attempt in range(1, retries + 1):
        try:
            r = httpx.post(
                DORIS_PROXY_URL,
                headers={"Content-Type": "application/json"},
                json=payload, timeout=timeout, verify=verify,
            )
            r.raise_for_status()
            d = r.json()
            if d.get("error") or d.get("success") is False:
                raise RuntimeError("Doris proxy rejected the query")
            rows = d.get("rows", d.get("data"))
            if isinstance(rows, list):
                return rows
            last_err = f"响应结构异常: {str(d)[:120]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
        if attempt < retries:
            time.sleep(attempt * 2)
    raise RuntimeError(f"Doris HTTP 代理查询失败: {last_err}")


def doris_query(sql: str, timeout: int = 120) -> list[dict]:
    if not all((DORIS_HOSTS, DORIS_USER, DORIS_PASSWORD, DORIS_DB)):
        raise ValueError("DORIS_HOSTS / DORIS_USER / DORIS_PASSWORD / DORIS_DB are required")
    rows = _query_direct(sql, timeout=timeout)
    if rows is not None:
        return rows
    return _query_proxy(sql, timeout=timeout)


def resolve_project_id(name_or_id: str) -> str:
    key = (name_or_id or "").strip()
    if not key:
        return ""
    with _pid_cache_lock:
        if key in _pid_cache:
            return _pid_cache[key]
    sql = (
        f"SELECT project_id, project_name FROM {T_MAPPING} "
        f"WHERE project_name = '{_sql_str(key)}' OR project_id = '{_sql_str(key)}' LIMIT 5"
    )
    rows = doris_query(sql, timeout=30)
    pid = ""
    if rows:
        exact = next((r for r in rows if r.get("project_name") == key), None)
        pid = (exact or rows[0]).get("project_id", "")
    if not pid:
        pid = key
    with _pid_cache_lock:
        _pid_cache[key] = pid
    return pid


# =====================================================================
# trace 元数据 / 详情拉取
# =====================================================================
def fetch_traces_meta(
    pid: str, date_from_str: str, date_to_str: str,
    trace_name: Optional[str] = None,
    limit: Optional[int] = None,
    session_id: str = "",
    user_id: str = "",
) -> list[dict]:
    name_cond = _trace_name_condition(trace_name)
    if session_id:
        name_cond += f" AND session_id = '{_sql_str(session_id)}'"
    if user_id:
        name_cond += f" AND user_id = '{_sql_str(user_id)}'"
    lim = limit if limit else 10000
    sql = (
        f"SELECT id, session_id, name, timestamp, input, user_id FROM {T_TRACES} "
        f"WHERE project_id = '{_sql_str(pid)}' AND is_deleted = 0"
        f" AND timestamp >= '{_sql_str(date_from_str)}' AND timestamp < '{_sql_str(date_to_str)}'"
        f"{name_cond} ORDER BY timestamp ASC LIMIT {int(lim)}"
    )
    return doris_query(sql, timeout=300)


def _build_trace_data(t: dict) -> dict:
    """从 trace 表行构造标准 trace_data 字典。"""
    return {
        "id": t.get("id"),
        "projectId": t.get("project_id"),
        "name": t.get("name"),
        "timestamp": _ts_to_iso(t.get("timestamp")),
        "environment": None,
        "tags": [],
        "userId": t.get("user_id"),
        "sessionId": t.get("session_id"),
        "input": _maybe_json(t.get("input") or ""),
        "output": _maybe_json(t.get("output") or ""),
        "metadata": {},
        "scores": [],
    }


def _build_observation(o: dict) -> dict:
    """从 observation 表行构造标准 observation 字典。"""
    return {
        "id": o.get("id"),
        "traceId": o.get("trace_id"),
        "type": o.get("type"),
        "name": o.get("name"),
        "startTime": _ts_to_iso(o.get("start_time")),
        "endTime": _ts_to_iso(o.get("end_time")),
        "input": _maybe_json(o.get("input") or "") or None,
        "output": _maybe_json(o.get("output") or "") or None,
        "metadata": None,
        "level": o.get("level"),
        "statusMessage": o.get("status_message"),
        "parentObservationId": o.get("parent_observation_id") or None,
        "model": o.get("provided_model_name"),
        "modelParameters": _maybe_json(o.get("model_parameters") or "") or None,
        "usage": None,
        "costDetails": None,
        "latency": None,
    }


def fetch_trace_full(trace_id: str) -> Optional[dict]:
    tid = _sql_str(trace_id)
    t_rows = doris_query(
        f"SELECT {_TRACE_COLS} FROM {T_TRACES} WHERE id = '{tid}' LIMIT 1", timeout=60
    )
    if not t_rows:
        return None
    t = t_rows[0]
    trace_data = _build_trace_data(t)
    o_rows = doris_query(
        f"SELECT {_OBS_COLS} FROM {T_OBSERVATIONS} "
        f"WHERE trace_id = '{tid}' AND is_deleted = 0 LIMIT 10000",
        timeout=180,
    )
    o_rows.sort(key=lambda o: o.get("start_time") or "")
    observations = [_build_observation(o) for o in o_rows]
    return {"trace": trace_data, "observations": observations}


def fetch_traces_full_batch(trace_ids: list[str]) -> list[dict]:
    """批量拉取多条 trace 的完整数据（trace + observations）。

    用 2 次 SQL（1 次 trace + 1 次 observations IN (...)）替代 N×2 次串行查询，
    显著降低 HTTP 代理往返延迟。返回顺序与 trace_ids 一致；缺失的 trace 被跳过。

    单批上限 10000 条 trace，observations 总量上限 100000 条。
    """
    ids = [str(tid) for tid in trace_ids if tid]
    if not ids:
        return []
    if len(ids) > 10000:
        raise ValueError("batch size exceeds 10000 traces")

    # 1 次查询拉所有 trace 行
    in_list = ",".join(f"'{_sql_str(tid)}'" for tid in ids)
    t_rows = doris_query(
        f"SELECT {_TRACE_COLS} FROM {T_TRACES} WHERE id IN ({in_list})", timeout=120
    )
    if not t_rows:
        return []
    t_map = {r.get("id"): r for r in t_rows}

    # 1 次查询拉所有 observations（列裁剪已对 GENERATION 的 input 置 NULL）
    o_rows = doris_query(
        f"SELECT {_OBS_COLS} FROM {T_OBSERVATIONS} "
        f"WHERE trace_id IN ({in_list}) AND is_deleted = 0 LIMIT 100000",
        timeout=300,
    )
    # 按 trace_id 分组
    o_map: dict[str, list] = {}
    for o in o_rows:
        tid = o.get("trace_id")
        if tid:
            o_map.setdefault(tid, []).append(o)

    # 按 trace_ids 顺序组装结果
    result = []
    for tid in ids:
        t = t_map.get(tid)
        if not t:
            continue
        trace_data = _build_trace_data(t)
        obs = o_map.get(tid, [])
        obs.sort(key=lambda o: o.get("start_time") or "")
        observations = [_build_observation(o) for o in obs]
        result.append({"trace": trace_data, "observations": observations})
    return result


# =====================================================================
# 会话类型判定（复刻 skill-opt 的 _session_kind）
# =====================================================================
def _session_kind(session_id: Optional[str]) -> str:
    if not session_id:
        return "unknown"
    if "dingtalk" in session_id:
        return "human"
    if session_id.startswith("cron:") or ":cron:" in session_id:
        return "cron"
    if session_id.startswith("cid"):
        return "human"
    if "openai" in session_id or "subagent" in session_id:
        return "subagent"
    if session_id.endswith(":main") or ":heartbeat" in session_id:
        return "heartbeat"
    return "other"


# =====================================================================
# build_adapter 入口
# =====================================================================
def build_adapter(config, options):
    """config — 完整 TeamEvolverConfig；options — datasource.options 字典。

    options 中可配置：
      project_id: Langfuse project_id（或项目名，会自动 resolve）
      trace_name: trace 名称过滤（如 openclaw-turn / agent-call）
    """
    return DorisSourceAdapter(config, options)


class DorisSourceAdapter:
    source_type = "doris"

    def __init__(self, config, options, mapper=None, session_mapper=None, meta_mapper=None):
        self.config = config
        self.options = options or {}
        self.project_id = self.options.get("project_id", "")
        self.trace_name = self.options.get("trace_name", "")
        self.mapper = mapper
        self.session_mapper = session_mapper
        # extract_meta(converted, session, traces) -> {user_id, session_id, trace_id}
        # 由 per-agent adapter 实现，供“运行总览”会话列表展示。
        self.meta_mapper = meta_mapper
        self._preview = {}

    def list_session_ids(self, filters, *, max_sessions):
        """按日期范围拉 trace 元数据，按 session_id 分组，返回 session_id 列表。

        过滤 subagent / heartbeat 噪声会话（与 skill-opt 默认口径一致）。
        上游没有 session_id 时，用 ``doris-trace:<trace id>`` 合成单 trace
        会话，避免这类 Agent 的全部数据在预览和拉取阶段被静默丢弃。
        """
        from_ts = str(filters.get("from_timestamp") or "")
        to_ts = str(filters.get("to_timestamp") or "")
        if not from_ts or not to_ts:
            raise ValueError("from_timestamp / to_timestamp are required")

        # 规范化为 UTC 字符串（Doris 表内 timestamp 存 UTC）
        df = self._normalize_ts(from_ts)
        dt = self._normalize_ts(to_ts)
        if df >= dt:
            raise ValueError("from_timestamp must precede to_timestamp")

        pid = resolve_project_id(self.project_id)
        if not pid:
            raise ValueError("project_id is required in the tenant adapter")

        # 优先用 adapter 配置的 trace_name（datasource.options），filters 里的
        # 是全局 langfuse_default_trace_name（如 openclaw-turn），可能不适用本项目
        trace_name = self.trace_name
        rows = fetch_traces_meta(pid, df, dt, trace_name, limit=10001,
                                 session_id=str(filters.get("session_id") or ""),
                                 user_id=str(filters.get("user_id") or ""))
        if len(rows) > 10000:
            raise ValueError("Trace scan exceeds 10000; narrow the time window")

        # 按 session 分组，过滤噪声
        sessions: dict[str, str] = {}  # sid -> 最早 timestamp
        self._preview.clear()
        for r in rows:
            upstream_sid = str(r.get("session_id") or "").strip()
            sid = upstream_sid or _synthetic_session_id(r.get("id"))
            if not sid:
                continue
            kind = _session_kind(upstream_sid) if upstream_sid else "other"
            if kind in ("subagent", "heartbeat"):
                continue
            if sid not in sessions:
                sessions[sid] = r.get("timestamp") or ""
                self._preview[sid] = {
                    "session_id": sid, "timestamp": _ts_to_iso(r.get("timestamp")),
                    "user_id": r.get("user_id") or "", "title": str(r.get("name") or ""),
                    "trace_count": 0,
                }
            self._preview[sid]["trace_count"] += 1

        # 按时间正序，截断到 max_sessions
        sorted_sids = sorted(sessions.keys(), key=lambda s: sessions[s])
        return sorted_sids[:max_sessions]

    def preview_sessions(self, filters, *, max_sessions):
        return [self._preview[sid] for sid in self.list_session_ids(filters, max_sessions=max_sessions)]

    def fetch_session(self, session_id):
        """拉取一个 session 的所有 trace（含 observations）。

        返回 (session_dict, traces_list)，traces_list 中每个元素是
        {"trace": {...}, "observations": [...]} 格式（与 Langfuse API 同构）。

        批量优化：1 次查 trace id 列表 + 1 次批量查 trace + 1 次批量查 observations
        （fetch_traces_full_batch），替代旧实现 N×2 次串行查询。
        """
        pid = resolve_project_id(self.project_id)
        if not pid:
            return {"id": session_id}, []

        # 1 次查询拉该 session 的所有 trace id（只取 id/name/timestamp/user_id，轻量）。
        # 无上游 session_id 的 trace 使用 list_session_ids 生成的合成 id，按 trace 主键查询。
        synthetic_trace_id = _synthetic_trace_id(session_id)
        where = (
            f"id = '{_sql_str(synthetic_trace_id)}'"
            if synthetic_trace_id
            else f"session_id = '{_sql_str(session_id)}'"
        )
        sql = (
            f"SELECT id, name, timestamp, user_id FROM {T_TRACES} "
            f"WHERE project_id = '{_sql_str(pid)}' AND {where}"
            f" AND is_deleted = 0"
            + _trace_name_condition(self.trace_name)
            + " ORDER BY timestamp ASC LIMIT 10001"
        )
        trace_rows = doris_query(sql, timeout=120)
        if len(trace_rows) > 10000:
            raise ValueError("Session exceeds 10000 traces")

        if not trace_rows:
            return {"id": session_id}, []

        # 批量拉取完整 trace + observations（2 次 SQL 搞定，不再 N×2 串行）
        trace_ids = [tr["id"] for tr in trace_rows if tr.get("id")]
        traces = fetch_traces_full_batch(trace_ids)

        session = {"id": session_id}
        first = trace_rows[0]
        session["user_id"] = first.get("user_id") or ""
        session["timestamp"] = _ts_to_iso(first.get("timestamp"))

        return session, traces

    def convert_session(self, session, traces):
        """将 Doris 拉取的 trace 数据转为 teamEvolver 标准 session 格式。

        复用 langfuse_convert 的转换逻辑（trace 结构与 Langfuse API 同构）。
        如果 adapter 提供了 session_mapper，在转换后调用以覆盖 title / user_alias 等。
        """
        from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session
        from session_ingestion.adapters._shared.langfuse_mapper import _deep_merge_turn

        def mapped(trace, observations, turn_num, defaults):
            patch = self.mapper(trace, observations, turn_num, defaults) if self.mapper else None
            return _deep_merge_turn(defaults, patch) if patch else defaults

        flattened = [{**item["trace"], "observations": item.get("observations") or []} for item in traces]
        converted = convert_langfuse_session(session, flattened, mapper=mapped)
        converted["source"] = "doris"
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
                logger.warning("[DorisAdapter] extract_meta failed; ignoring: %s", exc)
        return converted

    def health(self):
        try:
            if not self.project_id:
                raise ValueError("project_id is required")
            doris_query("SELECT 1 AS ok", timeout=10)
            pid = resolve_project_id(self.project_id) if self.project_id else ""
            return {"ok": True, "project_id": pid or self.project_id}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def close(self):
        pass

    @staticmethod
    def _normalize_ts(ts_str: str) -> str:
        """将 ISO 8601 时间字符串转为 Doris SQL 用的 UTC 字符串。"""
        s = ts_str.strip()
        if not s:
            return ""
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
