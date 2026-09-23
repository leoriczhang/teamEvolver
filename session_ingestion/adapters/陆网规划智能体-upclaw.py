# Adapter: 陆网规划智能体-upclaw
# 数据源 yaml: 陆网规划智能体-upclaw.yaml（inc-aiagent-core-skill-opt/config）
# 数据源类型: doris（统一走 Doris，project_id + 帐号密码即可跑）
# host=https://ai-langfuse.sf-express.com  project_id=（yaml 未配置，需从 Langfuse 映射表 resolve）
# trace_name=agent-call
# analyze={'cron_dedup': True, 'include_tool_calls': False}
# session MDS: rules.md, identity.md
# skills(35): ai-map-drawing, branch-efficiency, branch-line-re-schedule,
# branch-package, car-type-change, comb-chance-skill, ...
# workspace_source: eos-drsm-core-land-network-agent-upclaw.git
# branch=feature/202607/v1.0.5, skills_path=skills
# md_files_path: deploy-unit-config/env-prd/rules.md, deploy-unit-config/env-prd/identity.md
# experiment: gray_langfuse.host=https://ai-langfuse.sf-express.com
# evo: enabled=true, concurrency=3, require_tool_call=true
# dingtalk: app_key=dingn8rrwysdpcel4q5d, open_conversation_id=cid7k86AACuklc5ey2wplOZAg==

# 热重载：改完保存即生效，无需重启服务。
# 内置转换（langfuse_convert.convert_trace_to_turn）负责 prompt/response/tokens；
# 本文件 map_trace 只补充内置拿不到的字段（deep-merge 覆盖同名键）。

import json
import re

SOURCE = {
    "label": "陆网规划智能体-upclaw",
    "provider": "doris",
    "host": "agents-1-prd-doris.bdp.sfcloud.local",
    "project_id": "陆网规划",
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
    # 陆网规划智能体 (trace_name=agent-call):
    #   - SPAN "tool: <name>" observations -> tool_calls + tool_results
    #   - exec commands touching skills/<name>/ -> used_skills
    #   - skill names exposed in metadata.systemPrompt -> injected_skills
    tool_calls, tool_results, used_skills = _tool_spans(observations)
    out = {}
    from session_ingestion.adapters._shared.sf_agent_adapter import extract_mapping

    prompt_text = extract_mapping(
        trace,
        observations,
        agent="陆网规划智能体-upclaw",
    ).get("prompt_text", "")
    if prompt_text:
        out["prompt_text"] = prompt_text
    if tool_calls:
        out["tool_calls"] = tool_calls
        out["tool_results"] = tool_results
    injected = _injected_skills(trace)
    if injected:
        out["injected_skills"] = injected
    if used_skills:
        out["used_skills"] = used_skills
    return out or None


def _iter_json_objects(raw_input):
    """从多行文本里按花括号配平切出完整 JSON 对象。

    陆网规划的 input 是 FENCED_JSON 信封：```json``` 围栏包裹、跨多行
    pretty-print，不能按“单行以 { 开头”扫描。
    """
    if isinstance(raw_input, dict):
        yield raw_input
        return
    if not isinstance(raw_input, str) or not raw_input.strip():
        return
    text = raw_input
    depth = 0
    start = None
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict):
                        yield obj
                except (ValueError, TypeError):
                    pass
                start = None


def _first_user_line(prompt):
    """取第一行真实用户提问作标题：跳过围栏内内容、元信息头与结构化行。"""
    in_fence = False
    for raw in str(prompt or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line.lower().startswith("conversation info"):
            continue
        if line.startswith(("{", "[")) or line.endswith(":"):
            continue
        return line[:120]
    return ""


def _extract_sender_from_input(raw_input):
    """从 FENCED_JSON input 中提取真实工号（优先纯数字，跳过 user_ 前缀）。"""
    if not raw_input:
        return ""
    senders = []
    for obj in _iter_json_objects(raw_input):
        for key in ("sender", "sender_id", "user_id", "userId", "user"):
            value = str(obj.get(key) or "").strip()
            if value and "-" not in value:
                senders.append(value)
                break
        msg = obj.get("msg")
        if isinstance(msg, dict):
            for key in ("sender", "sender_id", "user_id", "userId"):
                value = str(msg.get(key) or "").strip()
                if value and "-" not in value:
                    senders.append(value)
                    break
    if not senders:
        return ""
    for s in senders:
        if s.isdigit():
            return s
    for s in senders:
        if not s.startswith("user_"):
            return s
    return senders[0]


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
    # 内置转换可能把 "Conversation info ..." 元信息头当成标题；一并兜底
    current_title = str(converted.get("title") or "").strip()
    title_is_bad = (
        not current_title
        or current_title.lower().startswith("conversation info")
        or current_title.startswith("```")
        or current_title.endswith("):")
    )
    if title_is_bad:
        for turn in converted.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            prompt = str(turn.get("prompt_text") or "").strip()
            if prompt:
                title = _first_user_line(prompt)
                if title:
                    out["title"] = title
                    break

    return out or None


def extract_meta(converted, session, traces):
    """Meta（user_id / session_id / trace_id），供“运行总览”展示。

    由用户按上游报文实现；trace_id 为用户在 metadata/input 中自定义的链路 id。
    """
    from session_ingestion.adapters._shared.session_meta import collect_session_meta

    return collect_session_meta(converted, session, traces, user_from_input=_extract_sender_from_input)
