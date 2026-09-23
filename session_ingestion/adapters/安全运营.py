# Adapter: 安全运营
# 数据源 yaml: 安全运营.yaml（inc-aiagent-core-skill-opt/config）
# 数据源类型: doris（统一走 Doris，project_id + 帐号密码即可跑）
# host=https://ai-langfuse.sf-express.com  project_id=cmqumpjhk02ufxa06r57zkl3h
# trace_name=harness.intent.runner,invoke_agent eos_pass_sois_intentRoutingAgent,POST /v1/chat/completions
# analyze={'cron_dedup': True}
# skills(0): workspace_source.type=upload（skills 未落盘）
# experiment: gray_langfuse.host=https://ai-langfuse.sf-express.com（无 agent_host）
# owner: 01442496, 01444722

# 热重载：改完保存即生效，无需重启服务。
# 内置转换（langfuse_convert.convert_trace_to_turn）负责 prompt/response/tokens；
# 本文件 map_trace 只补充内置拿不到的字段（deep-merge 覆盖同名键）。
#
# 安全运营的 trace 结构是 A2A 信封格式（invoke_agent parts / harness messages /
# POST 空链路），与标准 agent-call 不同：
#   - 顶层 input → user 文本（list(parts) / dict(messages) / 纯 str）
#   - 顶层 output → gpt 文本（assistant parts 取 text 正文）
#   - 思考块 <think>...</think> 需剥离，只留正文
#   - "tool:" SPAN → tool_calls/tool_results；systemPrompt → injected_skills

import json
import re

SOURCE = {
    "label": "安全运营",
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": "cmqumpjhk02ufxa06r57zkl3h",
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    from session_ingestion.adapters.sources.doris import DorisSourceAdapter
    return DorisSourceAdapter(
        None,
        {"project_id": SOURCE["project_id"], "trace_name": SOURCE.get("_trace_name", "")},
        mapper=map_trace,
        session_mapper=map_session,
        meta_mapper=extract_meta,
    )


# trace_name 是逗号多值，DorisSourceAdapter 会 split by comma 做 IN 查询
SOURCE["_trace_name"] = (
    "harness.intent.runner,"
    "invoke_agent eos_pass_sois_intentRoutingAgent,"
    "POST /v1/chat/completions"
)


def _obs_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(p for p in parts if p)
        if isinstance(content, str):
            return content
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, int, float, bool)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _tool_spans(observations):
    """"tool: <name>" SPAN 观测 → (tool_calls, tool_results, used_skills)。"""
    tool_calls = []
    tool_results = []
    used_skills = []
    for obs in observations or []:
        if not isinstance(obs, dict):
            continue
        name = str(obs.get("name") or "")
        if not name.startswith("tool:"):
            continue
        tool = name.split(":", 1)[1].strip()
        obs_id = str(obs.get("id") or "")
        raw_in = obs.get("input")
        if isinstance(raw_in, (dict, list)):
            cmd_src = json.dumps(raw_in, ensure_ascii=False)
        else:
            cmd_src = str(raw_in or "")
        tool_calls.append({
            "id": obs_id,
            "type": "function",
            "function": {"name": tool, "arguments": cmd_src},
        })
        text = _obs_text(obs.get("output"))
        level = str(obs.get("level") or "").upper()
        lowered = text.lower()
        has_error = (
            level == "ERROR"
            or "error" in lowered
            or "exception" in lowered
            or "traceback" in lowered
        )
        tool_results.append({
            "tool_call_id": obs_id,
            "tool_name": tool,
            "content": text,
            "has_error": has_error,
        })
        for m in re.findall(r"skills/([A-Za-z0-9_.-]+)", cmd_src):
            if m not in used_skills:
                used_skills.append(m)
    return tool_calls, tool_results, used_skills


def _injected_skills(trace):
    """metadata.systemPrompt 中暴露的 skill 名 → injected_skills。"""
    injected = []
    meta = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    sysprompt = str(meta.get("systemPrompt") or "")
    for m in re.findall(r"skills/([A-Za-z0-9_.-]+)/", sysprompt):
        if m not in injected:
            injected.append(m)
    for group in re.findall(r"skill（如 ([^）]+)）", sysprompt):
        for part in group.split("、"):
            p = part.strip()
            if p.endswith("等"):
                p = p[:-1].strip()
            if p and p not in injected:
                injected.append(p)
    return injected


# 思考块标签用拼接构造，避免本生成器源码出现完整字面量
_THINK_RE = re.compile("<" + "think>.*?<" + "/think>", re.S)


def _clean_text(t):
    """剥思考块，只留正文。"""
    return _THINK_RE.sub("", str(t or "")).strip()


def _parts_text(msgs, role=None):
    """A2A parts 消息数组（可选按 role 过滤）中所有 type=text 的 content 拼接后剥思考块。"""
    if not isinstance(msgs, list):
        return ""
    texts = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if role is not None and m.get("role") != role:
            continue
        for p in (m.get("parts") or []):
            if (
                isinstance(p, dict)
                and p.get("type") == "text"
                and str(p.get("content") or "").strip()
            ):
                texts.append(str(p["content"]))
    return _clean_text("\n".join(texts))


def _user_text(tin):
    """trace.input → 用户问题文本，兼容 list(parts) / dict(messages|content) / 纯 str。"""
    if isinstance(tin, list):
        return _parts_text(tin, role="user")
    if isinstance(tin, dict):
        msgs = tin.get("messages")
        if isinstance(msgs, list):
            texts = [
                str(m.get("content") or "")
                for m in msgs
                if isinstance(m, dict)
                and m.get("role") == "user"
                and str(m.get("content") or "").strip()
            ]
            if texts:
                return _clean_text("\n".join(texts))
        c = tin.get("content")
        if isinstance(c, str) and c.strip():
            return _clean_text(c)
        return ""
    if isinstance(tin, str):
        s = tin.strip()
        if s and not s.startswith("{"):
            return _clean_text(s)
    return ""


def _gpt_text(tout):
    """trace.output → gpt 回复文本（assistant parts 取 text 正文并剥思考块）。"""
    if isinstance(tout, list):
        return _parts_text(tout)
    if isinstance(tout, str):
        return _clean_text(tout.strip())
    return ""


def map_trace(trace, observations, turn_num=0, defaults=None):
    # A2A 信封结构（invoke_agent parts / harness messages / POST 空链路）：
    #   - 顶层 input → user 文本、output → gpt 文本（内置 str(list) 会成脏 repr，这里覆盖）
    #   - "tool:" SPAN → tool_calls/tool_results；systemPrompt → injected_skills
    user_text = _user_text(trace.get("input"))
    if not user_text:
        from session_ingestion.adapters._shared.sf_agent_adapter import (
            extract_mapping,
        )

        user_text = extract_mapping(
            trace,
            observations,
            agent="安全运营",
        ).get("prompt_text", "")
    gpt_text = _gpt_text(trace.get("output"))
    out = {}
    if user_text:
        out["prompt_text"] = user_text
    if gpt_text:
        out["response_text"] = gpt_text
    tool_calls, tool_results, used_skills = _tool_spans(observations)
    if tool_calls:
        out["tool_calls"] = tool_calls
        out["tool_results"] = tool_results
    injected = _injected_skills(trace)
    if injected:
        out["injected_skills"] = injected
    if used_skills:
        out["used_skills"] = used_skills
    return out or None


def map_session(converted, session, traces):
    """从上游 trace 数据中提取 title 和 user_alias。

    安全运营的 trace 使用 A2A 信封格式，user 标识可能藏在 input 的
    parts/messages 结构中。title 优先使用内置转换结果。
    """
    out = {}

    # --- title ---
    current = str(converted.get("title") or "").strip()
    title_is_bad = (
        not current
        or current.lower().startswith("post /")
        or current.lower().startswith("invoke_agent ")
    )
    if title_is_bad:
        for turn in converted.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            prompt = str(turn.get("prompt_text") or "").strip()
            if prompt:
                out["title"] = " ".join(prompt.split())[:120]
                break

    return out or None


# --------------------------------------------------------------------------- #
# Meta 信息（user_id / session_id / trace_id）—— 供“运行总览”会话列表展示
#
# 由用户按各自上游报文实现；缺字段返回空串即可（前端显示 “-”）。
# 安全运营为 A2A 信封：用户标识可能在 parts/messages 或 trace.metadata 中；
# trace_id 取用户在 metadata/input 里自定义的链路 id（非平台 trace 主键）。
# --------------------------------------------------------------------------- #

_USER_ID_KEYS = ("userId", "user_id", "userID", "sender", "sender_id", "empId", "emp_id", "user")


def _a2a_user_id(tin):
    """从 A2A parts/messages 信封里找用户标识；找不到返回 ""（交由通用深搜）。"""
    if isinstance(tin, list):
        for m in tin:
            if isinstance(m, dict) and m.get("role") == "user":
                for key in _USER_ID_KEYS:
                    if str(m.get(key) or "").strip():
                        return str(m[key]).strip()
                for part in m.get("parts") or []:
                    if isinstance(part, dict) and isinstance(part.get("metadata"), dict):
                        for key in _USER_ID_KEYS:
                            if str(part["metadata"].get(key) or "").strip():
                                return str(part["metadata"][key]).strip()
    elif isinstance(tin, dict):
        for key in _USER_ID_KEYS:
            if str(tin.get(key) or "").strip():
                return str(tin[key]).strip()
        msgs = tin.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict) and m.get("role") == "user":
                    for key in _USER_ID_KEYS:
                        if str(m.get(key) or "").strip():
                            return str(m[key]).strip()
    return ""


def extract_meta(converted, session, traces):
    """返回 {user_id, session_id, trace_id}；任一缺失给空串。"""
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        extract_meta as extract_sf_meta,
    )

    return extract_sf_meta(converted, session, traces, agent="安全运营")
