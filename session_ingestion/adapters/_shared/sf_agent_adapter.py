"""Shared Doris trace mapping for SF per-agent session adapters.

The upstream projects use several incompatible input envelopes.  This module
keeps the common extraction rules in one place so top-level tenant adapters
only declare SOURCE metadata and trace names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CONTEXT_TAG_RE = re.compile(r"<context_[^>]+>.*?</context_[^>]+>", re.DOTALL | re.IGNORECASE)
_REQUEST_CONTEXT_RE = re.compile(r"<request_context>.*?</request_context>", re.DOTALL | re.IGNORECASE)
_EMPLOYEE_IN_TEXT_RE = re.compile(r"(?:工号|员工号|emp(?:loyee)?(?:_?id)?)\s*[<:：=]?\s*(\d{5,12})", re.IGNORECASE)
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+")
_TRUNCATED_USER_RE = re.compile(
    r'"(?:role|type)"\s*:\s*"(?:user|human)"\s*,\s*"content"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)

_USER_KEYS = (
    "userids",
    "user_ids",
    "empcode",
    "emp_code",
    "empid",
    "emp_id",
    "employeenumber",
    "employee_id",
    "staffid",
    "staff_id",
    "usercode",
    "user_code",
    "senderuserid",
    "sender_user_id",
    "senderid",
    "sender_id",
    "userid",
    "user_id",
    "sender",
    "user",
)
_TRACE_ID_KEYS = (
    "agenttraceid",
    "customtraceid",
    "biztraceid",
    "businesstraceid",
    "externaltraceid",
    "traceid",
    "trace_id",
    "requestid",
    "request_id",
    "reqid",
    "req_id",
)
_PROMPT_KEYS = (
    "sys.query",
    "user_query",
    "userquery",
    "query",
    "question",
    "prompt",
    "message",
    "material_keyword",
    "keyword",
)
_TITLE_KEYS = (
    "title",
    "subject",
    "biztitle",
    "biz_title",
)
_USER_ROLES = {"user", "human"}
_FIXED_TITLES = {
    "客服智能助手": "客服工单结案标签分析",
    "流程智能体": "流程审批智能审核",
    "标签底盘": "客户标签查询",
}
_INTERNAL_PROMPT_MARKERS = (
    "基于用户输入和对话历史，识别当前请求的意图类型",
    "read heartbeat.md",
    "if nothing needs attention, reply heartbeat_ok",
)
_GENERIC_TITLE_NAMES = {
    "",
    "agent-call",
    "openclaw-turn",
    "trace",
    "turn",
    "session",
    "message",
    "langgraph",
    "a2a.receive",
    "spring_ai chat_client",
    "oc_agent_loop",
}


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_.]", "", str(value or "").lower())


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _iter_nodes(value: Any, *, depth: int = 0) -> Iterator[tuple[str, Any]]:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _iter_nodes(child, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_nodes(child, depth=depth + 1)


def _iter_json_objects(raw_input: Any) -> Iterator[dict[str, Any]]:
    """Yield JSON objects embedded in fenced or otherwise mixed text."""
    if isinstance(raw_input, dict):
        yield raw_input
        return
    if not isinstance(raw_input, str) or not raw_input.strip():
        return
    text = raw_input
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(text[start : index + 1])
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    yield parsed
                start = None


def _clean_text(value: Any) -> str:
    text = _THINK_RE.sub("", str(value or ""))
    return text.strip()


def _clean_prompt(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = _REQUEST_CONTEXT_RE.sub("", text).strip()
    if text.startswith("当前用户个人信息"):
        blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        if len(blocks) > 1:
            text = "\n\n".join(blocks[1:])
    text = re.sub(r"^用户问题[：:]\s*", "", text)
    text = re.sub(r"^【当前日期是[^】]*】\s*用户问[：:]\s*", "", text)
    text = re.sub(r"^\[subjob:[^\]]+\]\s*", "", text, flags=re.IGNORECASE)
    if "用户最新一轮对话：" in text:
        text = text.rsplit("用户最新一轮对话：", 1)[1].strip()
    if "文本内容:" in text:
        text = text.rsplit("文本内容:", 1)[1].strip()
    if text.startswith("[引用的消息"):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            text = lines[-1]
            text = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", text).strip()
    text = text.split("⚠️ **最高优先级指令**", 1)[0].strip()
    text = re.split(r"\n\s*\[定时任务上下文\]", text, maxsplit=1)[0].strip()
    text = re.split(r"[,，]\s*规则[：:]", text, maxsplit=1)[0].strip()
    user_lines = [
        re.sub(r"^USER[：:]\s*", "", line, flags=re.IGNORECASE).strip() for line in text.splitlines() if line.strip()
    ]
    if user_lines and all(
        re.match(r"^USER[：:]", line.strip(), flags=re.IGNORECASE) for line in text.splitlines() if line.strip()
    ):
        text = user_lines[-1]
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if len(blocks) > 1 and blocks[0].startswith("【语言要求】"):
        text = "\n\n".join(blocks[1:])
    return text.strip()


def _clean_title(value: Any) -> str:
    text = _clean_prompt(value)
    if not text:
        return ""
    text = _CONTEXT_TAG_RE.sub("", text).strip()
    parts: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.startswith("```"):
            continue
        if candidate.lower().startswith("conversation info"):
            continue
        if candidate.startswith(("{",)) or candidate.endswith("(untrusted metadata):"):
            continue
        candidate = _MARKDOWN_HEADING_RE.sub("", candidate).strip()
        if candidate:
            parts.append(candidate)
    return " ".join(parts)[:120]


def _first_user_line(raw_input: Any) -> str:
    """Return the first natural-language line outside fenced metadata blocks."""
    in_fence = False
    for raw_line in str(raw_input or "").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line.lower().startswith("conversation info"):
            continue
        if line.startswith(("{", "[")) or line.endswith("(untrusted metadata):"):
            continue
        return _clean_title(line)
    return ""


def _fenced_prompt(raw_input: Any) -> str:
    """Return all user-authored text outside fenced metadata blocks."""
    if not isinstance(raw_input, str):
        return ""
    in_fence = False
    lines: list[str] = []
    for raw_line in raw_input.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lower().startswith("conversation info"):
            continue
        if line or lines:
            lines.append(raw_line.rstrip())
    return _clean_prompt("\n".join(lines).strip())


def _text_from_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_text_from_content(item) for item in value]
        return "\n".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    for key in (*_PROMPT_KEYS, "text", "content"):
        for actual_key, child in value.items():
            if _norm_key(actual_key) == _norm_key(key):
                text = _text_from_content(child)
                if text:
                    return text
    return ""


def _role(message: dict[str, Any]) -> str:
    return str(message.get("role") or message.get("type") or "").strip().lower()


def _parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key in ("content", "text"):
            text = _text_from_content(part.get(key))
            if text:
                texts.append(text)
                break
    return _clean_text("\n".join(texts))


def _message_text(message: dict[str, Any]) -> str:
    parts = _parts_text(message.get("parts"))
    if parts:
        return parts
    return _text_from_content(message.get("content"))


def _messages_by_role(value: Any, roles: set[str]) -> list[str]:
    found: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            messages = node.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if (
                        isinstance(message, (list, tuple))
                        and len(message) >= 2
                        and str(message[0] or "").strip().lower() in roles
                    ):
                        text = _text_from_content(message[1])
                        if text:
                            found.append(text)
                        continue
                    if not isinstance(message, dict):
                        continue
                    role = _role(message)
                    if role in roles or (not role and roles == _USER_ROLES):
                        text = _message_text(message)
                        if text:
                            found.append(text)
                if found:
                    return
            role = _role(node)
            if role in roles and isinstance(node.get("parts"), list):
                text = _message_text(node)
                if text:
                    found.append(text)
                    return
            for child in node.values():
                visit(child, depth + 1)
                if found:
                    return
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
                if found:
                    return

    visit(value)
    return found


def _prompt_score(value: Any) -> int:
    text = _clean_prompt(value)
    if not text:
        return -10_000
    lowered = text.lower()
    score = min(len(text), 160)
    if text == "start":
        score -= 1000
    if "[truncated]" in lowered:
        score -= 300
    if any(marker in lowered for marker in _INTERNAL_PROMPT_MARKERS):
        score -= 1000
    if text.startswith("【语言要求】"):
        score -= 200
    if any(mark in text for mark in ("？", "?", "查询", "分析", "生成", "查看", "为什么", "怎么", "多少")):
        score += 80
    return score


def _best_prompt(values: Iterable[Any]) -> str:
    candidates = [_clean_prompt(value) for value in values]
    candidates = [value for value in candidates if value]
    if not candidates:
        return ""
    return max(enumerate(candidates), key=lambda item: (_prompt_score(item[1]), item[0]))[1]


def _truncated_json_user_prompts(value: str) -> list[str]:
    prompts: list[str] = []
    for match in _TRUNCATED_USER_RE.finditer(value):
        encoded = match.group(1)
        try:
            decoded = json.loads(f'"{encoded}"')
        except (TypeError, ValueError):
            decoded = encoded.replace(r"\n", "\n").replace(r"\"", '"')
        if str(decoded or "").strip():
            prompts.append(str(decoded).strip())
    return prompts


def _sfa2a_fields(raw_input: Any) -> dict[str, str]:
    parsed = _maybe_json(raw_input)
    if not isinstance(parsed, dict):
        return {}
    params = parsed.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict):
        return {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    user_ids: list[str] = []
    title = ""
    prompt = ""
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, dict):
            ids = text.get("userIds") or text.get("user_ids") or []
            if isinstance(ids, list):
                user_ids.extend(str(item).strip() for item in ids if str(item or "").strip())
            params_value = text.get("msgParam") or text.get("msg_param")
            if isinstance(params_value, dict):
                title = title or _scalar(params_value.get("title"))
                prompt = prompt or _scalar(params_value.get("text"))
            prompt = prompt or _text_from_content(text)
        elif isinstance(text, str):
            prompt = prompt or text.strip()
    user_ids.append(_scalar(metadata.get("senderUserId")))
    return {
        "user_id": _pick_user_id(user_ids),
        "title": _clean_title(title or prompt),
        "trace_id": _scalar(metadata.get("agentTraceId")),
        "prompt_text": _clean_text(prompt),
    }


def _candidate_values_with_paths(
    value: Any,
    keys: Iterable[str],
    *,
    prefix: str,
    depth: int = 0,
) -> list[tuple[str, str]]:
    if depth > 8:
        return []
    wanted = {_norm_key(key) for key in keys}
    candidates: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if _norm_key(key) in wanted:
                if isinstance(child, list):
                    candidates.extend((_scalar(item), f"{path}[]") for item in child if _scalar(item))
                elif _scalar(child):
                    candidates.append((_scalar(child), path))
            candidates.extend(_candidate_values_with_paths(child, keys, prefix=path, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            candidates.extend(
                _candidate_values_with_paths(
                    child,
                    keys,
                    prefix=f"{prefix}[{index}]",
                    depth=depth + 1,
                )
            )
    return candidates


def _looks_generated_user(value: str) -> bool:
    lowered = value.lower()
    if not value or set(value) == {"0"}:
        return True
    if lowered.startswith("user_"):
        return True
    if re.match(r"^unit\d*[_-]", lowered):
        return True
    if lowered.endswith(("-agent", "_agent")):
        return True
    if re.fullmatch(r"[0-9a-f]{32,}", lowered):
        return True
    return False


def _pick_user_id(candidates: Iterable[Any]) -> str:
    values = list(dict.fromkeys(_scalar(value) for value in candidates if _scalar(value)))
    for value in values:
        if value.isdigit() and set(value) != {"0"}:
            return value
    for value in values:
        if not _looks_generated_user(value) and "-" not in value and "." not in value:
            return value
    return ""


def _pick_user_with_source(candidates: Iterable[tuple[Any, str]]) -> tuple[str, str]:
    values = [(_scalar(value), source) for value, source in candidates if _scalar(value)]
    values = list(dict.fromkeys(values))
    for value, source in values:
        if value.isdigit() and set(value) != {"0"}:
            return value, source
    for value, source in values:
        if not _looks_generated_user(value) and "-" not in value and "." not in value:
            return value, source
    return "", ""


def _trusted_text_user_candidates(value: str, *, prefix: str) -> list[tuple[str, str]]:
    text = str(value or "")
    if not text:
        return []
    prompt_line = _first_user_line(text)
    prompt_match = re.match(
        r"^(?:用户)?工号\s*[<:：=]?\s*(\d{5,12})",
        prompt_line,
        re.IGNORECASE,
    )
    candidates = [(prompt_match.group(1), f"{prefix}:user-prompt-prefix")] if prompt_match else []
    trusted = (
        text.startswith("当前用户个人信息")
        or re.search(r"^\s*工号\s*<\s*\d+", text) is not None
        or re.search(r"用户工号\s*[:：]?\s*\d+", text) is not None
    )
    if not trusted:
        return candidates
    candidates.extend((match.group(1), f"{prefix}:text") for match in _EMPLOYEE_IN_TEXT_RE.finditer(text))
    return list(dict.fromkeys(candidates))


def _user_candidates_from_input(raw_input: Any, *, prefix: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    sfa2a = _sfa2a_fields(raw_input)
    if sfa2a.get("user_id"):
        candidates.append((sfa2a["user_id"], f"{prefix}.params.message.parts[].text.userIds"))
    parsed = _maybe_json(raw_input)
    if isinstance(parsed, (dict, list)):
        candidates.extend(_candidate_values_with_paths(parsed, _USER_KEYS, prefix=prefix))
        for path, child in _iter_nodes(parsed):
            if isinstance(child, str):
                candidates.extend(
                    _trusted_text_user_candidates(
                        child,
                        prefix=f"{prefix}.{path}",
                    )
                )
    if isinstance(raw_input, str):
        for index, obj in enumerate(_iter_json_objects(raw_input)):
            candidates.extend(
                _candidate_values_with_paths(
                    obj,
                    _USER_KEYS,
                    prefix=f"{prefix}.json[{index}]",
                )
            )
        candidates.extend(_trusted_text_user_candidates(raw_input, prefix=prefix))
    return candidates


def _trace_user_with_source(
    trace: dict[str, Any],
    session: dict[str, Any] | None = None,
    observations: list[Any] | None = None,
) -> tuple[str, str]:
    raw_input = trace.get("input")
    direct_candidates = _user_candidates_from_input(raw_input, prefix="trace.input")
    direct_structured = [item for item in direct_candidates if not item[1].endswith(":text")]
    direct_text = [item for item in direct_candidates if item[1].endswith(":text")]
    observation_candidates: list[tuple[str, str]] = []
    for index, observation in enumerate(observations or []):
        if not isinstance(observation, dict):
            continue
        observation_candidates.extend(
            _user_candidates_from_input(
                observation.get("input"),
                prefix=f"observations[{index}].input",
            )
        )
    observation_structured = [item for item in observation_candidates if not item[1].endswith(":text")]
    observation_text = [item for item in observation_candidates if item[1].endswith(":text")]
    candidates: list[tuple[str, str]] = list(direct_structured)
    candidates.append(
        (
            _scalar(trace.get("userId") or trace.get("user_id")),
            "trace.userId",
        )
    )
    if isinstance(session, dict):
        candidates.append((_scalar(session.get("user_id")), "session.user_id"))
    candidates.extend(observation_structured)
    candidates.extend(direct_text)
    candidates.extend(observation_text)
    session_id = _scalar(trace.get("sessionId") or trace.get("session_id"))
    candidates.extend(
        (value, "trace.sessionId")
        for value in re.split(r"[:_]", session_id)
        if value.isdigit() and 5 <= len(value) <= 12
    )
    return _pick_user_with_source(candidates)


def extract_user_id(
    trace: dict[str, Any],
    session: dict[str, Any] | None = None,
    observations: list[Any] | None = None,
) -> str:
    return _trace_user_with_source(trace, session, observations)[0]


def _direct_prompt(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for wanted in _PROMPT_KEYS:
        wanted_norm = _norm_key(wanted)
        for key, child in value.items():
            if _norm_key(key) != wanted_norm:
                continue
            text = _text_from_content(child)
            if text:
                return text
    return ""


def _business_prompt(agent: str, trace: dict[str, Any]) -> tuple[str, str]:
    raw_input = _maybe_json(trace.get("input"))
    if agent == "客户机会" and isinstance(raw_input, str):
        if raw_input.startswith("忽略历史对话，调用 opportunity-map-summary"):
            return "区域商机总结", "agent-profile.customer-opportunity-summary"
    if not isinstance(raw_input, dict):
        return "", ""
    if agent == "AI客服":
        messages = raw_input.get("raw_input_messages")
        if isinstance(messages, list):
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, dict):
                    continue
                order_type = _scalar(content.get("order_type"))
                reason = _scalar(content.get("cancelReason"))
                address = _scalar(content.get("order_address"))
                parts = [part for part in (order_type, reason, address) if part]
                if parts:
                    return "：".join(parts), f"trace.input.raw_input_messages[{index}].content"
    if agent == "信用助手":
        company = _scalar(raw_input.get("company"))
        if company:
            action = {
                "risk": "风险查询",
                "qualification": "资质查询",
            }.get(_scalar(trace.get("name")).lower(), "信用查询")
            return f"{company}{action}", "trace.input.company+trace.name"
    if agent == "流程智能体":
        prompt = _scalar(raw_input.get("preApprovalPrompt"))
        if prompt:
            match = re.search(r"你是一名\*{0,2}([^*\n]+?专家)\*{0,2}", prompt)
            if match:
                return match.group(1), "trace.input.preApprovalPrompt:role"
    return "", ""


def _prompt_from_input(raw_input: Any, *, prefix: str) -> tuple[str, str]:
    sfa2a = _sfa2a_fields(raw_input)
    if sfa2a.get("prompt_text"):
        return sfa2a["prompt_text"], f"{prefix}.params.message.parts[].text.msgParam.text"
    if isinstance(raw_input, str):
        parsed = _maybe_json(raw_input)
        if parsed is not raw_input:
            return _prompt_from_input(parsed, prefix=prefix)
        if "Conversation info" in raw_input or "```json" in raw_input:
            return _fenced_prompt(raw_input), f"{prefix}:fenced-text"
        truncated = _best_prompt(_truncated_json_user_prompts(raw_input))
        if truncated:
            return truncated, f"{prefix}:truncated-json-user-message"
        stripped = raw_input.lstrip()
        if stripped.startswith("{") or stripped.startswith(("[{", '["')):
            return "", ""
        return _clean_prompt(raw_input), prefix
    messages = _messages_by_role(raw_input, _USER_ROLES)
    if messages:
        best = _best_prompt(messages)
        if best and _prompt_score(best) > -500:
            return best, f"{prefix}.messages[user]"
    if isinstance(raw_input, dict):
        direct = _direct_prompt(raw_input)
        if direct:
            return _clean_prompt(direct), f"{prefix}:business-prompt-key"
        parts = _parts_text(raw_input.get("parts"))
        if parts:
            return _clean_prompt(parts), f"{prefix}.parts[]"
    if isinstance(raw_input, list):
        parts = _parts_text(raw_input)
        if parts:
            return _clean_prompt(parts), f"{prefix}[]"
    return "", ""


def _observation_prompt(
    observations: list[Any] | None,
) -> tuple[str, str]:
    candidates: list[tuple[int, str, str]] = []
    for index, observation in enumerate(observations or []):
        if not isinstance(observation, dict):
            continue
        name = _scalar(observation.get("name"))
        if name.startswith("tool:"):
            continue
        prompt, source = _prompt_from_input(
            observation.get("input"),
            prefix=f"observations[{index}].input",
        )
        if not prompt or _prompt_score(prompt) <= -500:
            continue
        lowered = name.lower()
        name_score = 0
        if "harness.intent.runner" in lowered:
            name_score = 500
        elif "agent turn summary" in lowered:
            name_score = 450
        elif lowered in {"a2a.receive", "a2a.send"}:
            name_score = 400
        elif str(observation.get("type") or "").upper() in {"AGENT", "CHAIN", "SPAN"}:
            name_score = 100
        candidates.append((name_score + _prompt_score(prompt), prompt, f"{source} ({name})"))
    if not candidates:
        return "", ""
    _, prompt, source = max(candidates, key=lambda item: item[0])
    return prompt, source


def extract_prompt_text(
    raw_input: Any,
    *,
    observations: list[Any] | None = None,
) -> str:
    prompt, _ = _prompt_from_input(raw_input, prefix="trace.input")
    if prompt and _prompt_score(prompt) > -500:
        return prompt
    observed, _ = _observation_prompt(observations)
    return observed


def _trace_prompt(
    trace: dict[str, Any],
    observations: list[Any] | None,
    *,
    agent: str,
) -> tuple[str, str]:
    business, source = _business_prompt(agent, trace)
    if business:
        return _clean_prompt(business), source
    direct, direct_source = _prompt_from_input(trace.get("input"), prefix="trace.input")
    observed, observed_source = _observation_prompt(observations)
    choices = [
        (_prompt_score(direct) + 300, direct, direct_source),
        (_prompt_score(observed), observed, observed_source),
    ]
    _score, prompt, prompt_source = max(choices, key=lambda item: item[0])
    return (prompt, prompt_source) if _score > -500 else ("", "")


def _trace_title_with_source(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
    *,
    agent: str = "",
) -> tuple[str, str]:
    if agent in _FIXED_TITLES:
        return _FIXED_TITLES[agent], "agent-profile.fixed-title"
    raw_input = trace.get("input")
    sfa2a = _sfa2a_fields(raw_input)
    if sfa2a.get("title"):
        return sfa2a["title"], "trace.input.params.message.parts[].text.msgParam.title"
    parsed = _maybe_json(raw_input)
    if isinstance(parsed, (dict, list)):
        explicit = _candidate_values_with_paths(parsed, _TITLE_KEYS, prefix="trace.input")
        if explicit:
            title = _clean_title(explicit[0][0])
            if title:
                return title, explicit[0][1]
    prompt, source = _trace_prompt(trace, observations, agent=agent)
    return _clean_title(prompt), source


def _is_generic_title(value: Any) -> bool:
    title = _clean_title(value)
    lowered = title.lower()
    return (
        not title
        or lowered in _GENERIC_TITLE_NAMES
        or lowered.startswith("post /")
        or lowered.startswith("invoke_agent ")
    )


def extract_title(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
    *,
    agent: str = "",
) -> str:
    return _trace_title_with_source(trace, observations, agent=agent)[0]


def _trace_id_with_source(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
) -> tuple[str, str]:
    raw_input = trace.get("input")
    sfa2a = _sfa2a_fields(raw_input)
    if sfa2a.get("trace_id"):
        return sfa2a["trace_id"], "trace.input.params.message.metadata.agentTraceId"
    for source_value, prefix in (
        (trace.get("metadata"), "trace.metadata"),
        (_maybe_json(raw_input), "trace.input"),
    ):
        if isinstance(source_value, (dict, list)):
            candidates = _candidate_values_with_paths(
                source_value,
                _TRACE_ID_KEYS,
                prefix=prefix,
            )
            if candidates:
                return candidates[0][0][:200], candidates[0][1]
    if isinstance(raw_input, str):
        for index, obj in enumerate(_iter_json_objects(raw_input)):
            candidates = _candidate_values_with_paths(
                obj,
                _TRACE_ID_KEYS,
                prefix=f"trace.input.json[{index}]",
            )
            if candidates:
                return candidates[0][0][:200], candidates[0][1]
    for index, observation in enumerate(observations or []):
        if not isinstance(observation, dict):
            continue
        observation_name = _scalar(observation.get("name")).lower()
        if not ("harness.intent.runner" in observation_name or observation_name in {"a2a.receive", "a2a.send"}):
            continue
        source_value = _maybe_json(observation.get("input"))
        if isinstance(source_value, (dict, list)):
            candidates = _candidate_values_with_paths(
                source_value,
                _TRACE_ID_KEYS,
                prefix=f"observations[{index}].input",
            )
            if candidates:
                return candidates[0][0][:200], candidates[0][1]
    return "", ""


def extract_trace_id(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
) -> str:
    return _trace_id_with_source(trace, observations)[0]


def _obs_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _tool_spans(observations: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    used_skills: list[str] = []
    for observation in observations or []:
        if not isinstance(observation, dict):
            continue
        name = _scalar(observation.get("name"))
        if not name.startswith("tool:"):
            continue
        tool = name.split(":", 1)[1].strip()
        observation_id = _scalar(observation.get("id"))
        raw_input = observation.get("input")
        arguments = (
            json.dumps(raw_input, ensure_ascii=False) if isinstance(raw_input, (dict, list)) else str(raw_input or "")
        )
        calls.append(
            {
                "id": observation_id,
                "type": "function",
                "function": {"name": tool, "arguments": arguments},
            }
        )
        content = _obs_text(observation.get("output"))
        lowered = content.lower()
        results.append(
            {
                "tool_call_id": observation_id,
                "tool_name": tool,
                "content": content,
                "has_error": str(observation.get("level") or "").upper() == "ERROR"
                or any(token in lowered for token in ("error", "exception", "traceback")),
            }
        )
        for skill in re.findall(r"skills/([A-Za-z0-9_.-]+)", arguments):
            if skill not in used_skills:
                used_skills.append(skill)
    return calls, results, used_skills


def _injected_skills(trace: dict[str, Any]) -> list[str]:
    metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    system_prompt = str(metadata.get("systemPrompt") or "")
    return list(dict.fromkeys(re.findall(r"skills/([A-Za-z0-9_.-]+)/", system_prompt)))


def map_trace(
    trace: dict[str, Any],
    observations: list[Any],
    turn_num: int = 0,
    defaults: dict[str, Any] | None = None,
    *,
    agent: str = "",
) -> dict[str, Any] | None:
    del turn_num, defaults
    patch: dict[str, Any] = {}
    prompt, _ = _trace_prompt(trace, observations, agent=agent)
    if prompt:
        patch["prompt_text"] = prompt
    calls, results, used_skills = _tool_spans(observations)
    if calls:
        patch["tool_calls"] = calls
        patch["tool_results"] = results
    if used_skills:
        patch["used_skills"] = used_skills
    injected = _injected_skills(trace)
    if injected:
        patch["injected_skills"] = injected
    return patch or None


def _trace_of(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    nested = item.get("trace")
    if isinstance(nested, dict):
        return nested
    return item if "input" in item else None


def _observations_of(item: Any) -> list[Any]:
    if not isinstance(item, dict):
        return []
    observations = item.get("observations")
    return observations if isinstance(observations, list) else []


def extract_mapping(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
    *,
    session: dict[str, Any] | None = None,
    agent: str = "",
) -> dict[str, str]:
    """Return extracted fields plus provenance for fixture auditing."""
    prompt, prompt_source = _trace_prompt(trace, observations, agent=agent)
    title, title_source = _trace_title_with_source(trace, observations, agent=agent)
    user, user_source = _trace_user_with_source(trace, session, observations)
    trace_id, trace_id_source = _trace_id_with_source(trace, observations)
    return {
        "prompt_text": prompt,
        "prompt_source": prompt_source,
        "title": title,
        "title_source": title_source,
        "user_id": user,
        "user_source": user_source,
        "trace_id": trace_id,
        "trace_id_source": trace_id_source,
    }


def map_session(
    converted: dict[str, Any],
    session: dict[str, Any],
    traces: list[Any],
    *,
    agent: str = "",
) -> dict[str, str] | None:
    trace_items = [(trace, _observations_of(item)) for item in traces if (trace := _trace_of(item)) is not None]
    patch: dict[str, str] = {}
    users = [extract_user_id(trace, session, observations) for trace, observations in trace_items]
    user = _pick_user_id(users)
    if user:
        patch["user_alias"] = user
    elif _looks_generated_user(_scalar(converted.get("user_alias"))):
        patch["user_alias"] = "anonymous"
    titles = [extract_title(trace, observations, agent=agent) for trace, observations in trace_items]
    title = next((candidate for candidate in titles if candidate), "")
    current = _clean_title(converted.get("title"))
    trace_names = {
        _clean_title(trace.get("name")).lower() for trace, _ in trace_items if _clean_title(trace.get("name"))
    }
    current_is_generic = _is_generic_title(current) or current.lower() in trace_names
    if title and not _is_generic_title(title):
        patch["title"] = title
    elif current and not current_is_generic:
        patch["title"] = current
    elif current:
        patch["title"] = ""
    return patch or None


def extract_meta(
    converted: dict[str, Any],
    session: dict[str, Any],
    traces: list[Any],
    *,
    agent: str = "",
) -> dict[str, str]:
    trace_items = [(trace, _observations_of(item)) for item in traces if (trace := _trace_of(item)) is not None]
    user = _scalar(converted.get("user_alias"))
    if not user:
        user = _pick_user_id(extract_user_id(trace, session, observations) for trace, observations in trace_items)
    session_id = next(
        (
            _scalar(trace.get("sessionId") or trace.get("session_id"))
            for trace, _ in trace_items
            if _scalar(trace.get("sessionId") or trace.get("session_id"))
        ),
        "",
    )
    session_id = session_id or _scalar(converted.get("session_id")) or _scalar(session.get("id"))
    trace_id = next(
        (value for trace, observations in trace_items if (value := extract_trace_id(trace, observations))),
        "",
    )
    return {"user_id": user, "session_id": session_id, "trace_id": trace_id}


def build_sf_doris_adapter(
    source: dict[str, Any],
    trace_name: str,
):
    from session_ingestion.adapters.sources.doris import DorisSourceAdapter

    return DorisSourceAdapter(
        None,
        {"project_id": source["project_id"], "trace_name": trace_name},
        mapper=lambda trace, observations, turn_num, defaults: map_trace(
            trace,
            observations,
            turn_num,
            defaults,
            agent=str(source.get("label") or ""),
        ),
        session_mapper=lambda converted, session, traces: map_session(
            converted,
            session,
            traces,
            agent=str(source.get("label") or ""),
        ),
        meta_mapper=lambda converted, session, traces: extract_meta(
            converted,
            session,
            traces,
            agent=str(source.get("label") or ""),
        ),
    )


def build_sf_langfuse_adapter(
    source: dict[str, Any],
    trace_name: str,
    options: dict[str, Any] | None = None,
):
    """Langfuse 直连版 build_sf_doris_adapter：字段映射完全一致，仅换传输层。

    连接默认走 LANGFUSE_PULL_HOST/PUBLIC_KEY/SECRET_KEY 环境变量（一套默认
    三元组只覆盖一个 Langfuse 项目）；其他项目在租户 adapter 文件里通过
    ``options`` 覆盖（值读 os.environ，密钥不写入 adapter 文件）。
    """
    from session_ingestion.adapters.sources.langfuse import LangfuseSource

    return LangfuseSource(
        None,
        {"trace_name": trace_name, **(options or {})},
        mapper=lambda trace, observations, turn_num, defaults: map_trace(
            trace,
            observations,
            turn_num,
            defaults,
            agent=str(source.get("label") or ""),
        ),
        session_mapper=lambda converted, session, traces: map_session(
            converted,
            session,
            traces,
            agent=str(source.get("label") or ""),
        ),
        meta_mapper=lambda converted, session, traces: extract_meta(
            converted,
            session,
            traces,
            agent=str(source.get("label") or ""),
        ),
    )
