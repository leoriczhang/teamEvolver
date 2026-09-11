"""Pure conversion functions vendored from the customer's skill-opt package.

Source: inc-aiagent-core-skill-opt/core/{langfuse_client,source_default,engine}.py.
Only pure functions are copied; no legacy service, globals or SDK clients run.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_SYS_INJECT_PREFIXES = ("System (untrusted):", "An async command you ran earlier")

def _clean_user_question(text: str) -> Optional[str]:
    """剥离 openclaw-turn 元数据信封，返回真实用户问题（移植自 question_cluster）。

    定位最后一个 ``` 围栏之后的内容；无信封则原样返回。空白压平。空内容返回 None。
    单字符（如选项回复"A"）是合法用户输入，不丢弃。
    """
    stripped = str(text or "").strip()
    fence_positions = [i for i in range(len(stripped)) if stripped.startswith("```", i)]
    if len(fence_positions) >= 2 and fence_positions[-1] > 0:
        body = stripped[fence_positions[-1] + 3:].strip()
    else:
        body = stripped
    body = " ".join(body.split()).strip()
    if not body:
        return None
    return body

def _parse_any_ts(ts: Optional[str]) -> datetime:
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = ts.strip()
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return datetime.min.replace(tzinfo=timezone.utc)

def parse_llm_output(output: Dict) -> List[Dict]:
    if not output or not isinstance(output, dict):
        return []
    content_list = output.get("content", [])
    if not isinstance(content_list, list):
        return []
    result = []
    for item in content_list:
        item_type = item.get("type")
        if item_type == "thinking":
            it = {"type": "thinking", "value": item.get("thinking", "")}
            if output.get("stopReason") in ("length", "max_tokens"):
                # 标记生成超长截断（耗尽输出 token 只产 thinking、无正文），
                # 供 _format_conversation 无 [gpt] 输出时区分「超长截断/无输出」占位
                it["stop_reason"] = "length"
            result.append(it)
        elif item_type == "text":
            result.append({"type": "text", "value": item.get("text", "")})
        elif item_type == "toolCall":
            result.append({"type": "toolCall", "name": item.get("name", ""),
                           "arguments": item.get("arguments", {})})
    return result

def parse_tool_output(output: Dict) -> List[Dict]:
    if not output or not isinstance(output, dict):
        return [{"type": "text", "value": ""}]
    # 格式 3：{content: "文本", isError: bool}
    if isinstance(output.get("content"), str):
        return [{"type": "text", "value": output["content"]}]
    content_list = output.get("content", [])
    if not isinstance(content_list, list):
        return [{"type": "text", "value": str(output.get("content", ""))}]
    result = []
    for item in content_list:
        # 实际数据里 content 列表可能混入裸 str/int 元素，容错处理避免整条 trace 分析失败
        if not isinstance(item, dict):
            if item is not None and str(item) != "":
                result.append({"type": "text", "value": str(item)})
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            if text:
                result.append({"type": "text", "value": text})
    if not result:
        details = output.get("details", {})
        if details:
            aggregated = details.get("aggregated", "")
            if aggregated:
                result.append({"type": "text", "value": aggregated})
            else:
                result.append({"type": "text", "value": json.dumps(details, ensure_ascii=False)})
        else:
            result.append({"type": "text", "value": ""})
    return result

def format_timestamp(iso_timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return iso_timestamp

def langfuse_to_template(trace_data: Dict) -> Dict:
    """Langfuse trace → 标准对话模板。"""
    trace = trace_data.get("trace", {})
    observations = trace_data.get("observations", [])
    parent_map: Dict[str, List[Dict]] = {}
    for obs in observations:
        pid = obs.get("parentObservationId")
        if pid:
            parent_map.setdefault(pid, []).append(obs)
    summary = next((o for o in observations if o.get("name") == "agent turn summary"), None)
    conversions: List[Dict] = []
    if summary:
        summary_id = summary.get("id")
        user_input = summary.get("input", "")
        if user_input:
            conversions.append({"role": "user",
                                "content": [{"type": "text", "value": user_input}],
                                "timestamp": format_timestamp(summary.get("startTime", ""))})
        loops = sorted((o for o in observations
                        if o.get("parentObservationId") == summary_id and "agent loop" in o.get("name", "")),
                       key=lambda x: x.get("startTime", ""))
        for loop in loops:
            loop_id = loop.get("id")
            loop_children = parent_map.get(loop_id, [])
            llm_requests = [c for c in loop_children if c.get("type") == "GENERATION"]
            tool_spans = [c for c in loop_children
                          if c.get("type") == "SPAN" and "tool:" in c.get("name", "")]
            gpt_content: List[Dict] = []
            for g in llm_requests:
                gpt_content += parse_llm_output(g.get("output", {}))
            if gpt_content:
                conversions.append({"role": "gpt", "content": gpt_content,
                                    "timestamp": format_timestamp(loop.get("startTime", ""))})
            for tool_span in sorted(tool_spans, key=lambda x: x.get("startTime", "")):
                conversions.append({"role": "tool_result",
                                    "content": parse_tool_output(tool_span.get("output", {})),
                                    "timestamp": format_timestamp(tool_span.get("startTime", "")),
                                    "observationId": tool_span.get("id", "")})
    elif any("agent" in (o.get("name") or "") and "loop" in (o.get("name") or "")
              for o in observations if not o.get("parentObservationId")):
        # 格式 4：agent-loop-x 直接挂 trace 下（无 agent turn summary 包裹），逐 loop 提取
        trace_input = trace.get("input", "")
        trace_start = trace.get("startTime", "") or trace.get("timestamp", "")
        if trace_input:
            conversions.append({"role": "user",
                                "content": [{"type": "text", "value": trace_input}],
                                "timestamp": format_timestamp(trace_start)})
        loops = sorted((o for o in observations
                        if not o.get("parentObservationId")
                        and "agent" in (o.get("name") or "") and "loop" in (o.get("name") or "")),
                       key=lambda x: x.get("startTime", ""))
        for loop in loops:
            loop_id = loop.get("id")
            loop_children = parent_map.get(loop_id, [])
            llm_requests = [c for c in loop_children if c.get("type") == "GENERATION"]
            tool_spans = [c for c in loop_children
                          if c.get("type") == "SPAN" and "tool:" in c.get("name", "")]
            gpt_content: List[Dict] = []
            for g in llm_requests:
                gpt_content += parse_llm_output(g.get("output", {}))
            if gpt_content:
                conversions.append({"role": "gpt", "content": gpt_content,
                                    "timestamp": format_timestamp(loop.get("startTime", ""))})
            for tool_span in sorted(tool_spans, key=lambda x: x.get("startTime", "")):
                conversions.append({"role": "tool_result",
                                    "content": parse_tool_output(tool_span.get("output", {})),
                                    "timestamp": format_timestamp(tool_span.get("startTime", "")),
                                    "observationId": tool_span.get("id", "")})
    elif (len([o for o in observations if o.get("type") == "GENERATION" and o.get("name") == "llm"]) > 1
          and not any(o.get("parentObservationId") for o in observations)):
        # 格式 3：扁平 agent-call（多个 llm GENERATION 无 parent 层级，按时间序交替）
        trace_input = trace.get("input", "")
        trace_start = trace.get("startTime", "") or trace.get("timestamp", "")
        if trace_input:
            conversions.append({"role": "user",
                                "content": [{"type": "text", "value": trace_input}],
                                "timestamp": format_timestamp(trace_start)})
        sorted_obs = sorted(observations, key=lambda x: x.get("startTime", ""))
        for obs in sorted_obs:
            if obs.get("type") == "GENERATION" and obs.get("name") == "llm":
                gpt_content = parse_llm_output(obs.get("output", {}))
                if gpt_content:
                    conversions.append({"role": "gpt", "content": gpt_content,
                                        "timestamp": format_timestamp(obs.get("startTime", ""))})
            elif obs.get("type") == "SPAN" and "tool:" in obs.get("name", ""):
                conversions.append({"role": "tool_result",
                                    "content": parse_tool_output(obs.get("output", {})),
                                    "timestamp": format_timestamp(obs.get("startTime", "")),
                                    "observationId": obs.get("id", "")})
            # 跳过 framework:/async-skill: 等系统 span
    else:
        # 格式 2：单个 llm GENERATION + tool SPAN（原始逻辑）
        trace_input = trace.get("input", "")
        trace_output = trace.get("output", "")
        trace_start = trace.get("startTime", "") or trace.get("timestamp", "")
        if trace_input:
            conversions.append({"role": "user",
                                "content": [{"type": "text", "value": trace_input}],
                                "timestamp": format_timestamp(trace_start)})
        llm = next((o for o in observations
                    if o.get("type") == "GENERATION" and o.get("name") == "llm"), None)
        tools = [o for o in observations if o.get("type") == "SPAN" and "tool:" in o.get("name", "")]
        if llm and tools:
            llm_output = llm.get("output", "")
            for tool in sorted(tools, key=lambda x: x.get("startTime", "")):
                tool_name = tool.get("name", "")
                if ":" in tool_name:
                    tool_name = tool_name.split(":", 1)[1].strip()
                tool_input = tool.get("input", {})
                conversions.append({"role": "gpt", "content": [{
                    "type": "toolCall", "name": tool_name,
                    "arguments": tool_input if isinstance(tool_input, dict) else {}}],
                    "timestamp": format_timestamp(tool.get("startTime", "")),
                    "observationId": tool.get("id", "")})
                conversions.append({"role": "tool_result", "content": parse_tool_output(tool.get("output", {})),
                                    "timestamp": format_timestamp(tool.get("endTime", tool.get("startTime", ""))),
                                    "observationId": tool.get("id", "")})
            if llm_output:
                # output 可能是 {"role": "assistant", "content": [...]} 消息结构：
                # 按 content items 解析；否则 JSON 序列化，避免 dict 被 str() 成 Python repr
                parsed = parse_llm_output(llm_output) if isinstance(llm_output, dict) else []
                if parsed:
                    conversions.append({"role": "gpt", "content": parsed,
                                        "timestamp": format_timestamp(llm.get("endTime", llm.get("startTime", "")))})
                else:
                    val = llm_output if isinstance(llm_output, str) else json.dumps(llm_output, ensure_ascii=False)
                    conversions.append({"role": "gpt", "content": [{"type": "text", "value": val}],
                                        "timestamp": format_timestamp(llm.get("endTime", llm.get("startTime", "")))})
        elif trace_output:
            val = trace_output if isinstance(trace_output, str) else json.dumps(trace_output, ensure_ascii=False)
            conversions.append({"role": "gpt", "content": [{"type": "text", "value": val}],
                                "timestamp": format_timestamp(trace.get("endTime", trace_start))})
    # 兜底：Langfuse 同步 Doris 有 span 延迟（如 agent-loop 下末尾 GENERATION 未同步），
    # 各格式分支可能提不出完整收尾：全程无 gpt 文本，或末轮不是带文本的 gpt（停在 tool_result/空 gpt）。
    # 此时 trace.output 通常已有最终答案，直接补一轮 gpt，避免 analyze 误判（fallback 标记供诊断）。
    trace_output = trace.get("output", "")
    def _is_gpt_text(c: Dict) -> bool:
        return c.get("role") == "gpt" and any(
            i.get("type") == "text" and str(i.get("value") or "").strip()
            for i in (c.get("content") or []))
    has_gpt_text = any(_is_gpt_text(c) for c in conversions)
    last_is_gpt_text = bool(conversions) and _is_gpt_text(conversions[-1])
    if (not has_gpt_text or not last_is_gpt_text) and trace_output:
        val = trace_output if isinstance(trace_output, str) else json.dumps(trace_output, ensure_ascii=False)
        conversions.append({"role": "gpt", "content": [{"type": "text", "value": val}],
                            "timestamp": format_timestamp(trace.get("endTime") or trace.get("timestamp", "")),
                            "fallback": "trace.output"})
    return {"traceId": trace.get("id", ""), "sessionId": trace.get("sessionId", ""),
            "timestamp": format_timestamp(trace.get("timestamp", "")),
            "conversions": conversions,
            "scores": trace.get("scores") or []}

def _session_kind(session_id: Optional[str]) -> str:
    """按 session_id 前缀判 openclaw 会话发起方。

    注意 user_id 不可靠：'main' 和 null 在各桶混布（人/定时器/子agent 都可能出现 null），
    只能用 session_id 判。session_id 形如 ``agent:main:<source>:<id>``，<source> 决定发起方：
    - dingtalk（dingtalk-connector / dingtalk:default:direct）→ 人
    - cron                                   → 定时器
    - openai / subagent                      → 框架内部子 agent（含「友好提示」等内部 LLM 调用）
    - 结尾 :main 或含 :heartbeat             → 心跳（新版格式形如 agent:main:main:heartbeat）
    - agent:internal:*                       → 内部
    """
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

def _extract_first_text(trace_input) -> str:
    """从 trace.list() 返回的 trace.input 提取首条用户文本（用于 web 列表展示）。

    先剥 openclaw 元数据信封再截断（若先截断，信封会占满截断长度，真实问题被截掉）。
    按序扫描 user 消息，取第一条清洗后有实质内容的；系统注入消息（heartbeat/async 回调）跳过。
    """
    if not trace_input:
        return ""
    candidates: List[str] = []
    if isinstance(trace_input, str):
        candidates.append(trace_input)
    elif isinstance(trace_input, dict):
        # 常见结构：{"content": "..."} 或 {"messages": [{"role":"user","content":"..."}]}
        if "content" in trace_input and isinstance(trace_input["content"], str):
            candidates.append(trace_input["content"])
        msgs = trace_input.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content", "")
                    candidates.append(c if isinstance(c, str) else str(c))
        # openclaw 消息信封：{"sender":..., "msg":{"type":"text|audio|file|...","content":{...}}}
        # text 类型真实问题在 content.query；audio 等富媒体类型 ASR/描述文本在 content.text；
        # file 类型无文本内容，用 fileName 兜底展示
        msg = trace_input.get("msg")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, dict):
                q = content.get("query") or content.get("text")
                if not q and content.get("fileName"):
                    q = f"[文件] {content['fileName']}"
                if q:
                    candidates.append(str(q))
            elif isinstance(content, str):
                candidates.append(content)
    else:
        candidates.append(str(trace_input))
    for raw in candidates:
        if str(raw or "").lstrip().startswith(_SYS_INJECT_PREFIXES):
            continue   # 系统注入消息，非真实用户问题
        q = _clean_user_question(raw)
        if q:
            # 孤代理字符（上游截断 emoji 残留）无法编码 UTF-8，写索引前剔除
            q = re.sub(r"[\ud800-\udfff]", "\ufffd", q)
            return q[:300]
    return ""

def _dedup_cron_traces(traces_meta: Dict[str, Dict]) -> Dict[str, Dict]:
    """定时任务（cron session）同一天内输入完全一样，每个 session 只保留最早一条。

    人工对话（cid/dingtalk 等）不受影响。实测网点智能体单日 32382 条 cron → 1717 条，
    整体质检量减少约 90%。
    """
    cron_first: Dict[str, Tuple[Optional[datetime], str]] = {}  # sid -> (ts, tid)
    drop: List[str] = []
    for tid, meta in traces_meta.items():
        sid = meta.get("sessionId") or ""
        if _session_kind(sid) != "cron":
            continue
        ts = _parse_any_ts(meta.get("timestamp"))
        cur = cron_first.get(sid)
        if cur is None:
            cron_first[sid] = (ts, tid)
        elif ts is not None and (cur[0] is None or ts < cur[0]):
            drop.append(cur[1])
            cron_first[sid] = (ts, tid)
        else:
            drop.append(tid)
    if drop:
        for tid in drop:
            traces_meta.pop(tid, None)
        print(f"[fetch] cron 去重：{len(cron_first)} 个定时任务 session 各保留首条，"
              f"过滤 {len(drop)} 条重复 trace（CRON_DEDUP=1）")
    return traces_meta

def default_extract_fields(trace_input: Any, meta: Dict, ctx: Any) -> Dict:
    """工号(emp_id) + 首文本(first_text) 抽取（默认实现）。

    - first_text：逐字复用 _extract_first_text（与重构前 fetch 建索引口径一致）。
    - emp_id：优先 meta['userId']（Langfuse trace.list 带 user_id），缺失时回退
      sessionId 末段（复刻 feedback join 的 `sid.split(":")[-1]`；Doris 元数据无 user_id 列）。
      emp_id 是 trace_index 的**可选附加键**（additive，下游读未知键忽略）。

    入参 meta 里可能带一个瞬态 'userId'（由 default_query_meta 注入，仅供抽取，不落 trace_index）。
    自定义脚本可覆写本钩子实现每源不同的工号/首文本抽取。
    """
    first_text = _extract_first_text(trace_input)
    uid = str((meta or {}).get("userId") or "").strip()
    if not uid:
        sid = (meta or {}).get("sessionId") or ""
        uid = sid.split(":")[-1] if sid else ""
    return {"first_text": first_text, "emp_id": uid}

def default_filter_meta(traces_meta: Dict[str, Dict], ctx: Any) -> Dict[str, Dict]:
    """源特定噪声过滤（默认实现）：剔除 subagent / heartbeat 会话。

    复刻重构前 _run_fetch_doris / _fetch_langfuse_meta_one 里的
    `if not all_sessions and _session_kind(sid) in ("subagent","heartbeat"): continue` 语义。
    ctx.all_sessions=True 时不剔除（全保留）。

    说明：默认 query_meta 在取数时已内联剔除过一遍（Langfuse 分页需据此估算保留率），
    本钩子再过滤一次是**幂等**的（对已过滤集合为 no-op），目的是给「自定义 query_meta
    返回未过滤数据」的场景兜底，并作为过滤逻辑的规范覆写点。
    """
    if getattr(ctx, "all_sessions", False):
        return traces_meta
    kept = {t: m for t, m in traces_meta.items()
            if _session_kind(m.get("sessionId")) not in ("subagent", "heartbeat")}
    removed = len(traces_meta) - len(kept)
    if removed:
        print(f"[fetch] filter_meta 剔除 {removed} 条 subagent/heartbeat 噪声")
    return kept

def default_dedup(traces_meta: Dict[str, Dict], ctx: Any) -> Dict[str, Dict]:
    """cron 当日去重（默认实现）：受 ctx.cron_dedup 门控，复刻重构前 run_fetch 的开关语义。

    注意：门控**只作用于本默认钩子**。项目自定义 dedup 钩子总是执行（用户自管开关，
    可自读 ctx.config / ctx.load_existing_sessions() 做增量去重）。
    """
    if getattr(ctx, "cron_dedup", False):
        return _dedup_cron_traces(traces_meta)
    return traces_meta

def default_convert(trace_data: Dict) -> Dict:
    """trace → conv 转换（默认实现）：委托 langfuse_to_template（与默认转换器一致）。"""
    return langfuse_to_template(trace_data)

