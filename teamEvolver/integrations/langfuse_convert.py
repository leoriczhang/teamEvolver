"""Convert Langfuse sessions/traces into the teamEvolver session format.

These functions are intentionally pure (no network, no config) so the mapping
logic can be unit-tested with fixture payloads. They translate the Langfuse v3
public-API shapes into the same session dict that ``/ingest_session`` and
``SessionStore`` already consume from Hermes/AgentsHub:

    Langfuse session            -> teamEvolver session
    Langfuse trace (chronological) -> one interaction turn
    Langfuse GENERATION obs      -> model/api call + token usage
    OpenAI-style tool_calls / role="tool" messages -> tool_calls / tool_results

The Langfuse data model is generic, so message extraction copes with the common
encodings we see in practice: a bare string, a single ``{role, content}`` map, a
``{"messages": [...]}`` wrapper, a list of message maps, and list-style content
parts (``[{"type": "text", "text": "..."}]``).
"""

from __future__ import annotations

import json
from typing import Any

# Cap any single text body so ingested payloads stay reasonable, mirroring the
# Hermes push hook (push_session.MAX_CHARS). Keep this generous for 256k+
# context models so downstream summarization preserves enough raw evidence.
MAX_CHARS = 64_000
MAX_SYSTEM_CHARS = 200_000

# Langfuse core observation types (see commons.yml ObservationType).
_GENERATION_TYPES = {"GENERATION"}
_EVENT_TYPES = {"EVENT"}
_TOOL_ROLES = {"tool", "function"}


def _extract_text(value: Any) -> str:
    """Flatten arbitrary Langfuse content into a single text string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # OpenAI-style content parts: {"type": "text", "text": "..."}.
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif "content" in item:
                    parts.append(_extract_text(item.get("content")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _extract_text(value.get("content"))
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or item.get("name") or "").strip()
        arguments = function.get("arguments")
        if arguments is None:
            arguments = item.get("arguments")
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            {
                "id": str(item.get("id") or item.get("tool_call_id") or ""),
                "type": str(item.get("type") or "function"),
                "function": {"name": name, "arguments": str(arguments or "{}")},
            }
        )
    return calls


def _normalize_messages(value: Any, *, default_role: str) -> list[dict[str, Any]]:
    """Return a list of normalized message dicts from arbitrary Langfuse IO."""
    if value is None:
        return []
    if isinstance(value, dict):
        inner = value.get("messages")
        if isinstance(inner, list):
            return _normalize_messages(inner, default_role=default_role)
        if any(key in value for key in ("role", "content", "tool_calls")):
            value = [value]
        else:
            return [{"role": default_role, "content": _extract_text(value)[:MAX_CHARS]}]
    if isinstance(value, (str, int, float, bool)):
        text = _extract_text(value)[:MAX_CHARS]
        return [{"role": default_role, "content": text}] if text else []
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            text = _extract_text(item)[:MAX_CHARS]
            if text:
                messages.append({"role": default_role, "content": text})
            continue
        role = str(item.get("role") or default_role)
        content_source = item.get("content") if "content" in item else item
        message: dict[str, Any] = {
            "role": role,
            "content": _extract_text(content_source)[:MAX_CHARS],
        }
        tool_calls = _normalize_tool_calls(item.get("tool_calls"))
        if tool_calls:
            message["tool_calls"] = tool_calls
        if role in _TOOL_ROLES:
            message["tool_call_id"] = str(item.get("tool_call_id") or "")
            message["tool_name"] = str(item.get("name") or item.get("tool_name") or "")
        messages.append(message)
    return messages


def _observation_tokens(obs: dict[str, Any]) -> tuple[int, int, int]:
    """Return (input, output, total) token counts for one observation."""

    def _as_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    usage_details = obs.get("usageDetails") if isinstance(obs.get("usageDetails"), dict) else {}
    usage = obs.get("usage") if isinstance(obs.get("usage"), dict) else {}
    input_tokens = _as_int(
        usage_details.get("input")
        or usage_details.get("prompt_tokens")
        or usage_details.get("input_tokens")
        or usage.get("input")
    )
    output_tokens = _as_int(
        usage_details.get("output")
        or usage_details.get("completion_tokens")
        or usage_details.get("output_tokens")
        or usage.get("output")
    )
    total_tokens = _as_int(
        usage_details.get("total")
        or usage_details.get("total_tokens")
        or usage.get("total")
    )
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _has_error(obs: dict[str, Any]) -> bool:
    if str(obs.get("level") or "").upper() == "ERROR":
        return True
    status = str(obs.get("statusMessage") or "").lower()
    return any(token in status for token in ("error", "exception", "failed", "traceback"))


def _tool_calls_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            calls.append(call)
    return calls


def _tool_results_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for message in messages:
        if str(message.get("role") or "") not in _TOOL_ROLES:
            continue
        content = str(message.get("content") or "")
        results.append(
            {
                "tool_call_id": message.get("tool_call_id") or "",
                "tool_name": message.get("tool_name") or "",
                "content": content,
                "has_error": any(
                    token in content.lower()
                    for token in ("error", "exception", "traceback", "failed")
                ),
            }
        )
    return results


def convert_trace_to_turn(
    trace: dict[str, Any],
    turn_num: int,
) -> dict[str, Any]:
    """Convert one Langfuse trace (with full observations) into a turn dict."""
    observations = trace.get("observations") if isinstance(trace.get("observations"), list) else []

    input_messages = _normalize_messages(trace.get("input"), default_role="user")
    output_messages = _normalize_messages(trace.get("output"), default_role="assistant")

    # Observations carry the finer-grained conversation (per-generation IO and
    # tool spans). Fold generation IO into the message stream so tool_calls /
    # tool results surface even when the trace-level output omits them.
    observation_messages: list[dict[str, Any]] = []
    input_tokens = output_tokens = total_tokens = 0
    generation_count = 0
    non_generation_spans = 0
    models: list[str] = []
    for obs in sorted(
        (o for o in observations if isinstance(o, dict)),
        key=lambda o: str(o.get("startTime") or ""),
    ):
        obs_type = str(obs.get("type") or "").upper()
        if obs_type in _GENERATION_TYPES:
            generation_count += 1
            model = str(obs.get("model") or "").strip()
            if model:
                models.append(model)
            in_tok, out_tok, tot_tok = _observation_tokens(obs)
            input_tokens += in_tok
            output_tokens += out_tok
            total_tokens += tot_tok
            observation_messages.extend(
                _normalize_messages(obs.get("input"), default_role="user")
            )
            observation_messages.extend(
                _normalize_messages(obs.get("output"), default_role="assistant")
            )
        elif obs_type not in _EVENT_TYPES:
            # SPAN / TOOL / AGENT / CHAIN style observations represent
            # intermediate steps; treat named ones as tool activity.
            non_generation_spans += 1

    combined_messages = input_messages + observation_messages + output_messages
    tool_calls = _tool_calls_from_messages(combined_messages)
    tool_results = _tool_results_from_messages(combined_messages)

    prompts = [m["content"] for m in input_messages if m["role"] == "user" and m["content"]]
    if not prompts:
        prompts = [
            m["content"]
            for m in combined_messages
            if m["role"] == "user" and m["content"]
        ]
    responses = [
        m["content"] for m in output_messages if m["role"] == "assistant" and m["content"]
    ]
    if not responses:
        responses = [
            m["content"]
            for m in combined_messages
            if m["role"] == "assistant" and m["content"]
        ]

    # Prefer explicit tool_calls; otherwise fall back to counting non-generation
    # observation spans as tool activity so metrics are non-zero for agents that
    # log tool executions as spans rather than message tool_calls.
    tool_call_count = len(tool_calls) or non_generation_spans

    return {
        "turn_num": turn_num,
        "trace_id": str(trace.get("id") or ""),
        "prompt_text": "\n".join(prompts).strip()[:MAX_CHARS],
        "response_text": "\n".join(responses).strip()[:MAX_CHARS],
        "messages": combined_messages,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "injected_skills": [],
        "used_skills": [],
        "read_skills": [],
        "modified_skills": [],
        "metrics": {
            "tool_call_count": tool_call_count,
            "api_call_count": generation_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens or (input_tokens + output_tokens),
            "message_tokens": total_tokens or (input_tokens + output_tokens),
        },
        "_langfuse": {
            "trace_name": str(trace.get("name") or ""),
            "user_id": str(trace.get("userId") or ""),
            "tags": list(trace.get("tags") or []),
            "release": str(trace.get("release") or ""),
            "version": str(trace.get("version") or ""),
            "environment": str(trace.get("environment") or ""),
            "timestamp": str(trace.get("timestamp") or ""),
            "models": models,
        },
    }


def _dominant(values: list[str]) -> str:
    if not values:
        return ""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get)


def convert_langfuse_session(
    session: dict[str, Any],
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert a Langfuse session plus its full traces into a teamEvolver session.

    ``session`` is the object returned by ``GET /api/public/sessions/{id}`` (or a
    minimal ``{"id": ...}`` when only the id is known). ``traces`` is a list of
    ``TraceWithFullDetails`` (each including its ``observations``), which the
    caller has already fetched and may pre-filter/sort.
    """
    session_id = str(session.get("id") or session.get("session_id") or "").strip()

    ordered_traces = sorted(
        (t for t in traces if isinstance(t, dict)),
        key=lambda t: str(t.get("timestamp") or ""),
    )

    turns: list[dict[str, Any]] = []
    for index, trace in enumerate(ordered_traces, start=1):
        turns.append(convert_trace_to_turn(trace, index))

    total_input = sum(int(turn["metrics"]["input_tokens"]) for turn in turns)
    total_output = sum(int(turn["metrics"]["output_tokens"]) for turn in turns)
    total_tokens = sum(int(turn["metrics"]["total_tokens"]) for turn in turns)
    tool_call_count = sum(int(turn["metrics"]["tool_call_count"]) for turn in turns)
    api_call_count = sum(int(turn["metrics"]["api_call_count"]) for turn in turns)

    messages: list[dict[str, Any]] = []
    for turn in turns:
        messages.extend(turn["messages"])

    user_ids = [str(t.get("userId") or "").strip() for t in ordered_traces if t.get("userId")]
    environments = [
        str(t.get("environment") or "").strip() for t in ordered_traces if t.get("environment")
    ]
    releases = [str(t.get("release") or "").strip() for t in ordered_traces if t.get("release")]
    versions = [str(t.get("version") or "").strip() for t in ordered_traces if t.get("version")]
    models = [
        model
        for turn in turns
        for model in (turn.get("_langfuse", {}).get("models") or [])
    ]
    tags: list[str] = []
    for trace in ordered_traces:
        for tag in trace.get("tags") or []:
            tag = str(tag).strip()
            if tag and tag not in tags:
                tags.append(tag)

    # Session title: first trace name, else first user prompt.
    title = ""
    if ordered_traces:
        title = str(ordered_traces[0].get("name") or "").strip()
    if not title:
        for turn in turns:
            if turn.get("prompt_text"):
                title = turn["prompt_text"].splitlines()[0][:120]
                break

    timestamp = str(
        session.get("createdAt")
        or session.get("timestamp")
        or (ordered_traces[0].get("timestamp") if ordered_traces else "")
        or ""
    )

    converted: dict[str, Any] = {
        "session_id": session_id,
        "turns": turns,
        "messages": messages,
        "system_prompt": "",
        "injected_skills": [],
        "used_skills": [],
        "source": "langfuse",
        "model": _dominant([m for turn in turns for m in _turn_models(turn)]),
        "metrics": {
            "interaction_turns": len(turns),
            "message_count": len(messages),
            "tool_call_count": tool_call_count,
            "api_call_count": api_call_count,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens or (total_input + total_output),
        },
        "langfuse": {
            "session_id": session_id,
            "project_id": str(session.get("projectId") or ""),
            "environment": _dominant(environments),
            "environments": sorted(set(environments)),
            "user_id": _dominant(user_ids),
            "user_ids": sorted(set(user_ids)),
            "tags": tags,
            "release": _dominant(releases),
            "version": _dominant(versions),
            "trace_count": len(ordered_traces),
            "trace_ids": [str(t.get("id") or "") for t in ordered_traces],
        },
    }
    if title:
        converted["title"] = title
    if timestamp:
        converted["timestamp"] = timestamp
    dominant_user = _dominant(user_ids)
    if dominant_user:
        converted["user_alias"] = dominant_user
    return converted


def _turn_models(turn: dict[str, Any]) -> list[str]:
    langfuse = turn.get("_langfuse") if isinstance(turn.get("_langfuse"), dict) else {}
    models = langfuse.get("models")
    return [str(m) for m in models] if isinstance(models, list) else []
