# Adapter: 速运小智
# 数据源 yaml: 速运小智.yaml（inc-aiagent-core-skill-opt/config）
# 数据源类型: doris（统一走 Doris，project_id + 帐号密码即可跑）
# host=https://ai-langfuse.sf-express.com  project_id=cmrlu3fon0009va06uwxe0xu6
# trace_name=agent-call
# analyze={'include_tool_calls': False}
# skills(24): 场地规划与项目智能体, 大件智能助手, 大件航空, 安全运营, 客户运维, 快件质量, 智能问数 ...
# workspace_source: o2o-dds-iss-express-one-agent.git
# skills_path=agent-core/src/main/java/express/one/agent/localSkills
# experiment: rollout_concurrency=5

# 热重载：改完保存即生效，无需重启服务。
# 内置转换（langfuse_convert.convert_trace_to_turn）负责 prompt/response/tokens；
# 本文件 map_trace 只补充内置拿不到的字段（deep-merge 覆盖同名键）。

import json
import re

SOURCE = {
    "label": "速运小智",
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": "cmrlu3fon0009va06uwxe0xu6",
    "enabled": True,
    "max_sessions": 100,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    from session_ingestion.adapters.sources.doris import DorisSourceAdapter

    return DorisSourceAdapter(
        None,
        {"project_id": SOURCE["project_id"], "trace_name": "agent-call"},
        mapper=map_trace,
        session_mapper=map_session,
        meta_mapper=extract_meta,
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
    """ "tool: <name>" SPAN 观测 → (tool_calls, tool_results, used_skills)。"""
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
        tool_calls.append(
            {
                "id": obs_id,
                "type": "function",
                "function": {"name": tool, "arguments": cmd_src},
            }
        )
        text = _obs_text(obs.get("output"))
        level = str(obs.get("level") or "").upper()
        lowered = text.lower()
        has_error = level == "ERROR" or "error" in lowered or "exception" in lowered or "traceback" in lowered
        tool_results.append(
            {
                "tool_call_id": obs_id,
                "tool_name": tool,
                "content": text,
                "has_error": has_error,
            }
        )
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
    # 速运小智 (trace_name=agent-call):
    #   - SPAN "tool: <name>" observations -> tool_calls + tool_results
    #   - exec commands touching skills/<name>/ -> used_skills
    #   - skill names exposed in metadata.systemPrompt -> injected_skills
    tool_calls, tool_results, used_skills = _tool_spans(observations)
    out = {}
    if tool_calls:
        out["tool_calls"] = tool_calls
        out["tool_results"] = tool_results
    injected = _injected_skills(trace)
    if injected:
        out["injected_skills"] = injected
    if used_skills:
        out["used_skills"] = used_skills
    return out or None


def _extract_sender_from_input(raw_input):
    """从 trace input 中提取真实用户标识。"""
    if not raw_input:
        return ""
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
    if not str(converted.get("title") or "").strip():
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
    from session_ingestion.adapters._shared.sf_agent_adapter import (
        extract_meta as extract_sf_meta,
    )

    return extract_sf_meta(converted, session, traces, agent="速运小智")
