"""Shared trace mapping for Doris and Langfuse session adapters.

The upstream projects use several incompatible input envelopes.  This module
keeps the common extraction rules in one place so top-level tenant adapters
only declare SOURCE metadata and trace names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .trace_mapping_keys import (
    _CONTEXT_TAG_RE,
    _GENERIC_TITLE_NAMES,
    _INTERNAL_PROMPT_MARKERS,
    _MARKDOWN_HEADING_RE,
    _PROMPT_KEYS,
    _REQUEST_CONTEXT_RE,
    _THINK_RE,
    _TITLE_KEYS,
    _TRACE_ID_KEYS,
    _TRUNCATED_USER_RE,
    _USER_KEYS,
    _USER_ROLES,
)


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

def iter_json_objects(raw_input: Any) -> Iterator[dict[str, Any]]:
    """Public adapter helper for JSON objects embedded in mixed text."""
    yield from _iter_json_objects(raw_input)

def _clean_text(value: Any) -> str:
    text = _THINK_RE.sub("", str(value or ""))
    return text.strip()

def _clean_prompt(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = _REQUEST_CONTEXT_RE.sub("", text).strip()
    user_lines = [
        re.sub(r"^USER[：:]\s*", "", line, flags=re.IGNORECASE).strip() for line in text.splitlines() if line.strip()
    ]
    if user_lines and all(
        re.match(r"^USER[：:]", line.strip(), flags=re.IGNORECASE) for line in text.splitlines() if line.strip()
    ):
        text = user_lines[-1]
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
        if not _looks_generated_user(value):
            return value
    return ""

def _pick_user_with_source(candidates: Iterable[tuple[Any, str]]) -> tuple[str, str]:
    values = [(_scalar(value), source) for value, source in candidates if _scalar(value)]
    values = list(dict.fromkeys(values))
    for value, source in values:
        if not _looks_generated_user(value):
            return value, source
    return "", ""

def _user_candidates_from_input(raw_input: Any, *, prefix: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    parsed = _maybe_json(raw_input)
    if isinstance(parsed, (dict, list)):
        candidates.extend(_candidate_values_with_paths(parsed, _USER_KEYS, prefix=prefix))
    if isinstance(raw_input, str):
        for index, obj in enumerate(_iter_json_objects(raw_input)):
            candidates.extend(
                _candidate_values_with_paths(
                    obj,
                    _USER_KEYS,
                    prefix=f"{prefix}.json[{index}]",
                )
            )
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

def _prompt_from_input(raw_input: Any, *, prefix: str) -> tuple[str, str]:
    if isinstance(raw_input, str):
        parsed = _maybe_json(raw_input)
        if parsed is not raw_input:
            return _prompt_from_input(parsed, prefix=prefix)
        if "Conversation info" in raw_input or "```json" in raw_input:
            embedded = list(_iter_json_objects(raw_input))
            for index in range(len(embedded) - 1, -1, -1):
                candidates = _candidate_values_with_paths(
                    embedded[index],
                    _PROMPT_KEYS,
                    prefix=f"{prefix}.json[{index}]",
                )
                prompt = _best_prompt(value for value, _path in candidates)
                if prompt and _prompt_score(prompt) > -500:
                    return prompt, f"{prefix}.json[{index}]:json-envelope"
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

def _trace_prompt(
    trace: dict[str, Any],
    observations: list[Any] | None,
) -> tuple[str, str]:
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
) -> tuple[str, str]:
    raw_input = trace.get("input")
    parsed = _maybe_json(raw_input)
    if isinstance(parsed, (dict, list)):
        explicit = _candidate_values_with_paths(parsed, _TITLE_KEYS, prefix="trace.input")
        if explicit:
            title = _clean_title(explicit[0][0])
            if title:
                return title, explicit[0][1]
    prompt, source = _trace_prompt(trace, observations)
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
) -> str:
    return _trace_title_with_source(trace, observations)[0]

def _trace_id_with_source(
    trace: dict[str, Any],
    observations: list[Any] | None = None,
) -> tuple[str, str]:
    raw_input = trace.get("input")
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

def _collect_tool_spans(
    observations: list[Any],
    output_text: Callable[[Any], str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
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
        content = output_text(observation.get("output"))
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

def _tool_spans(observations: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    return _collect_tool_spans(observations, _obs_text)

def _rich_observation_text(value: Any) -> str:
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            parts = [
                item["text"]
                if isinstance(item, dict) and isinstance(item.get("text"), str)
                else json.dumps(item, ensure_ascii=False)
                for item in content
            ]
            return "\n".join(part for part in parts if part)
        if isinstance(content, str):
            return content
    if isinstance(value, (list, int, float, bool)):
        return json.dumps(value, ensure_ascii=False)
    return _obs_text(value)

def tool_spans(observations: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Convert tool observations while preserving rich content payloads."""
    return _collect_tool_spans(observations, _rich_observation_text)

def _injected_skills(trace: dict[str, Any]) -> list[str]:
    metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
    system_prompt = str(metadata.get("systemPrompt") or "")
    return list(dict.fromkeys(re.findall(r"skills/([A-Za-z0-9_.-]+)/", system_prompt)))

def map_trace(
    trace: dict[str, Any],
    observations: list[Any],
    turn_num: int = 0,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    del turn_num, defaults
    patch: dict[str, Any] = {}
    prompt, _ = _trace_prompt(trace, observations)
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
) -> dict[str, str]:
    """Return extracted fields plus provenance for fixture auditing."""
    prompt, prompt_source = _trace_prompt(trace, observations)
    title, title_source = _trace_title_with_source(trace, observations)
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
) -> dict[str, str] | None:
    trace_items = [(trace, _observations_of(item)) for item in traces if (trace := _trace_of(item)) is not None]
    patch: dict[str, str] = {}
    users = [extract_user_id(trace, session, observations) for trace, observations in trace_items]
    user = _pick_user_id(users)
    if user:
        patch["user_alias"] = user
    elif _looks_generated_user(_scalar(converted.get("user_alias"))):
        patch["user_alias"] = "anonymous"
    titles = [extract_title(trace, observations) for trace, observations in trace_items]
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
