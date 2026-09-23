# Adapter: 客户智能体-upclaw
# 数据源 yaml: 客户智能体-upclaw.yaml（inc-aiagent-core-skill-opt/config）
# 数据源类型: doris（统一走 Doris，project_id + 帐号密码即可跑）
# host=https://ai-langfuse.sf-express.com  project_id=cmnzt155t00w8xa06hxfgc7p2
# trace_name=openclaw-turn
# analyze={'include_tool_calls': False}
# skills(12): assurance-agent, customer-name-fuzzy-match,
# customer-portrait-onepager, generate-customer-plan, guard-agent, ...
# workspace_source: inc-aiagent-core-customer-oc.git
# skills_path=deployment/workspace/skills/skills-prd-oc
# experiment: agent_host=http://customer-upclaw.int.sfcloud.local:1080, rollout_concurrency=5

# 热重载：改完保存即生效，无需重启服务。
# 内置转换（langfuse_convert.convert_trace_to_turn）负责 prompt/response/tokens；
# 本文件 map_trace 只补充内置拿不到的字段（deep-merge 覆盖同名键）。

import json
import re

SOURCE = {
    "label": "客户智能体-upclaw", "provider": "doris", "host": "agents-1-prd-doris.bdp.sfcloud.local", "project_id": "cmnzt155t00w8xa06hxfgc7p2",
    "enabled": True, "max_sessions": 100,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    from session_ingestion.adapters.sources.doris import DorisSourceAdapter

    return DorisSourceAdapter(
        None,
        {"project_id": SOURCE["project_id"], "trace_name": "openclaw-turn"},
        mapper=map_trace,
        session_mapper=map_session,
        meta_mapper=extract_meta,
    )


_HEARTBEAT_MARK = "An async command you ran earlier has completed"


def _is_heartbeat(trace):
    sid = str(trace.get("sessionId") or "").lower()
    if "heartbeat" in sid:
        return True
    text = trace.get("input")
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    return _HEARTBEAT_MARK in str(text or "")


_ZEROED_TURN = {
    "prompt_text": "",
    "response_text": "",
    "messages": [],
    "tool_calls": [],
    "tool_results": [],
    "used_skills": [],
    "injected_skills": [],
    "metrics": {
        "tool_call_count": 0,
        "api_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "message_tokens": 0,
    },
}


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


def map_trace(trace, observations, turn_num=0, defaults=None):
    # openclaw-turn mapping:
    #   - heartbeat traces -> zeroed turn
    #   - SPAN "tool: <name>" observations -> tool_calls + tool_results
    #   - exec commands touching skills/<name>/ -> used_skills
    #   - skill names exposed in metadata.systemPrompt -> injected_skills
    if _is_heartbeat(trace):
        return dict(_ZEROED_TURN)
    tool_calls, tool_results, used_skills = _tool_spans(observations)
    out = {
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "used_skills": used_skills,
        "injected_skills": _injected_skills(trace),
    }
    # SFA2A 信封的用户正文在 msgParam.text，内置转换取不到，这里覆盖
    sf = _sfa2a_fields(trace.get("input"))
    if sf.get("prompt_text"):
        out["prompt_text"] = sf["prompt_text"]
    return out


def _sfa2a_message(raw_input):
    """从 SFA2A/2.0 jsonrpc 信封取 params.message（dict）；非该结构返回 None。"""
    obj = raw_input
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    params = obj.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    return message if isinstance(message, dict) else None


def _sfa2a_fields(raw_input):
    """解析 SFA2A 信封 -> {user_id, title, trace_id, prompt_text}。

    客户智能体-upclaw 走 A2A over jsonrpc：真实用户在 parts[].text.userIds，
    标题在 msgParam.title，发起方 senderUserId(000000) 仅回退，trace_id 取
    metadata.agentTraceId。
    """
    message = _sfa2a_message(raw_input)
    if message is None:
        return {}
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    user_ids = []
    title = ""
    prompt_text = ""
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, dict):
            for uid in text.get("userIds") or []:
                user_ids.append(str(uid).strip())
            msg_param = text.get("msgParam")
            if isinstance(msg_param, dict):
                title = title or str(msg_param.get("title") or "").strip()
                prompt_text = prompt_text or str(msg_param.get("text") or "").strip()
            if not prompt_text and isinstance(text.get("text"), str):
                prompt_text = text["text"].strip()
        elif isinstance(text, str) and not prompt_text:
            prompt_text = text.strip()
    user_id = ""
    for uid in user_ids:
        if uid.isdigit() and set(uid) != {"0"}:
            user_id = uid
            break
    if not user_id:
        user_id = next((u for u in user_ids if u), "") or str(meta.get("senderUserId") or "").strip()
    if not title and prompt_text:
        title = prompt_text.splitlines()[0][:120]
    return {
        "user_id": user_id,
        "title": title[:120],
        "trace_id": str(meta.get("agentTraceId") or "").strip(),
        "prompt_text": prompt_text,
    }


def _extract_sender_from_input(raw_input):
    """从 trace input 中提取真实用户标识。

    openclaw-turn 的 input 格式：
      {"sender": "user_xxx", "msg": {"content": {"query": "..."}}}
    """
    if not raw_input:
        return ""
    _sf = _sfa2a_fields(raw_input)
    if _sf.get("user_id"):
        return _sf["user_id"]
    if isinstance(raw_input, str):
        if raw_input[:1] in "{[":
            try:
                raw_input = json.loads(raw_input)
            except (ValueError, TypeError):
                return ""
        else:
            return ""
    if isinstance(raw_input, dict):
        for key in ("sender", "sender_id", "user_id", "userId", "user"):
            value = str(raw_input.get(key) or "").strip()
            if value and "-" not in value:
                return value
        msg = raw_input.get("msg")
        if isinstance(msg, dict):
            for key in ("sender", "sender_id", "user_id", "userId"):
                value = str(msg.get(key) or "").strip()
                if value and "-" not in value:
                    return value
    return ""


def map_session(converted, session, traces):
    """从上游 trace 数据中提取 title 和 user_alias。"""
    out = {}

    # --- user_alias ---
    for item in traces:
        trace = item.get("trace") if isinstance(item, dict) else None
        if not isinstance(trace, dict):
            continue
        sender = _extract_sender_from_input(trace.get("input"))
        if sender:
            out["user_alias"] = sender
            break
    if "user_alias" not in out:
        uid = str(session.get("user_id") or "").strip() if isinstance(session, dict) else ""
        if uid and "-" not in uid and "." not in uid:
            out["user_alias"] = uid

    # --- title ---
    # SFA2A 信封的 msgParam.title 是干净标题，优先于内置从正文首行提取的结果
    for item in traces:
        trace = item.get("trace") if isinstance(item, dict) else None
        if not isinstance(trace, dict):
            continue
        sf = _sfa2a_fields(trace.get("input"))
        if sf.get("title"):
            out["title"] = sf["title"]
            break
    if "title" not in out and not str(converted.get("title") or "").strip():
        for turn in converted.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            prompt = str(turn.get("prompt_text") or "").strip()
            if prompt:
                for line in prompt.splitlines():
                    line = line.strip()
                    if line and not line.startswith(("{", "[")):
                        out["title"] = line[:120]
                        break
                if "title" in out:
                    break

    return out or None


def extract_meta(converted, session, traces):
    """Meta（user_id / session_id / trace_id），供“运行总览”展示。

    由用户按上游报文实现；trace_id 为用户在 metadata/input 中自定义的链路 id。
    """
    from session_ingestion.adapters._shared.session_meta import (
        DEFAULT_TRACE_ID_KEYS,
        collect_session_meta,
    )

    meta = collect_session_meta(
        converted, session, traces, user_from_input=_extract_sender_from_input,
        # SFA2A 的自定义链路 id 在 metadata.agentTraceId
        trace_id_keys=("agentTraceId",) + tuple(DEFAULT_TRACE_ID_KEYS),
    )
    # trace_id 深搜可能命中 metadata 里的其它 id；SFA2A 明确用 agentTraceId
    if not meta.get("trace_id"):
        for item in traces:
            trace = item.get("trace") if isinstance(item, dict) else None
            if not isinstance(trace, dict):
                continue
            sf = _sfa2a_fields(trace.get("input"))
            if sf.get("trace_id"):
                meta["trace_id"] = sf["trace_id"]
                break
    return meta
