"""Complete MA events, usage and turn boundaries shared by consumers."""

from __future__ import annotations

import json
import re

PREVIEW = {"event_start", "event_delta", "bridge.ready"}
TOOL_USES = {"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"}
THREAD_MESSAGES = {"agent.thread_message_sent", "agent.thread_message_received", "session.thread_created"}


def tag_values(tags):
    """MA tags are key/value objects; retain legacy normalized input support."""
    return [str(tag["key"]) + ":" + str(tag["value"])
            if isinstance(tag, dict) and "key" in tag and "value" in tag else tag
            for tag in tags or [] if isinstance(tag, str) or
            (isinstance(tag, dict) and "key" in tag and "value" in tag)]


async def parse_sse(lines):
    data, name, ident = [], "", ""
    async for line in lines:
        if line.startswith(":"):
            if line[1:].strip() == "ready":
                yield {"type": "bridge.ready"}
        elif not line:
            if data:
                value = json.loads("\n".join(data))
                if not isinstance(value, dict):
                    raise ValueError("MA event must be an object")
                yield {"type": name, "id": ident, **value}
            data, name, ident = [], "", ""
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("id:"):
            ident = line[3:].strip()
    if data:
        yield {"type": name, "id": ident, **json.loads("\n".join(data))}


def complete(events):
    unique = {}
    for event in events:
        if event.get("id") and event.get("type") not in PREVIEW:
            unique[event["id"]] = event
    # MA history is ordered; preserve ordering and replace queued with processed.
    return list(unique.values())


def text(event):
    content = event.get("content") or []
    if isinstance(content, str):
        return content
    return "\n".join(str(block.get("text", "")) for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def stable_end(events):
    if not events:
        return None
    statuses = [e for e in events if e.get("type", "").startswith("session.status_")
                and not e.get("session_thread_id")]
    if not statuses:
        return None
    end = statuses[-1]
    if end["type"] == "session.status_terminated":
        return end
    if end["type"] == "session.status_idle" and end.get("stop_reason", {}).get("type") in {
        "end_turn", "retries_exhausted",
    }:
        return end
    return None


def child_threads(main, threads):
    """MA lists the root thread too; its first running event precedes delegation."""
    root = next((e.get("session_thread_id") for e in main
                 if e.get("type") == "session.thread_status_running"), None)
    return [t for t in threads if t["id"] != root]


def metrics(events):
    events = complete(events)
    calls = [e for e in events if e.get("type") == "span.model_request_end"]
    result = {"tool_call_count": sum(e.get("type") in TOOL_USES for e in events)}
    if not calls:
        return result
    result["api_calls"] = len(calls)
    values = [c.get("model_usage") or {} for c in calls]
    fields = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
              "cache_write_tokens": "cache_creation_input_tokens", "cache_read_tokens": "cache_read_input_tokens"}
    for key, source in fields.items():
        if all(type(v.get(source)) is int and v[source] >= 0 for v in values):
            result[key] = sum(v[source] for v in values)
    if all(key in result for key in fields):
        result["total_tokens"] = sum(result[key] for key in fields)
    return result


def _new_turn(prompt="", timestamp=None):
    return {"prompt_text": prompt, "response_text": "", "timestamp": timestamp,
            "messages": [], "tool_calls": [], "tool_results": [], "tool_errors": [],
            "used_skills": [], "skill_refs": [], "event_ids": []}


def _used_skill_names(arguments, names, paths, tool_name=""):
    """Resolve recorded file paths; the model still judges semantic relevance."""
    raw = json.dumps(arguments, ensure_ascii=False)
    found = set()
    if tool_name == "skill" and isinstance(arguments, dict):
        selected = arguments.get("name")
        if selected in names.values():
            found.add(selected)
    for ident, name in names.items():
        if any(re.search(r"(?:^|/)skills/" + re.escape(value) + r"(?:/|[\"'\s]|$)", raw)
               for value in (ident, name)):
            found.add(name)
    for prefix, name in (paths or {}).items():
        if prefix.rstrip("/") + "/" in raw:
            found.add(name)
    return sorted(found)


def turns(events, skill_names=None, skill_paths=None):
    """Project protocol structure; semantic Skill recognition remains LLM-owned."""
    result, current = [], None
    names = skill_names or {}
    for event in complete(events):
        kind = event.get("type")
        thread_id = event.get("session_thread_id") or ""
        if kind == "user.message" and not event.get("session_thread_id"):
            current = {**_new_turn(text(event), event.get("processed_at")),
                       "turn_num": len(result) + 1, "trace_id": event["id"], "agent_threads": [],
                       "attachments": [b for b in event.get("content", []) if b.get("type") != "text"]}
            current["messages"].append({"role": "user", "content": text(event), "event_id": event["id"]})
            current["event_ids"].append(event["id"])
            result.append(current)
        elif current is not None:
            thread = None
            if thread_id:
                thread = next((t for t in current["agent_threads"] if t["session_thread_id"] == thread_id), None)
                if thread is None:
                    thread = {**_new_turn(timestamp=event.get("processed_at")),
                              "session_thread_id": thread_id, "agent_name": event.get("agent_name", ""),
                              "trace_id": current["trace_id"] + ":thread:" + thread_id,
                              "parent_trace_id": current["trace_id"], "parent_turn_num": current["turn_num"]}
                    current["agent_threads"].append(thread)
                if event.get("agent_name"):
                    thread["agent_name"] = event["agent_name"]
            targets = [current] + ([thread] if thread is not None else [])
            identity = {"event_id": event["id"], "session_thread_id": thread_id,
                        "agent_name": event.get("agent_name", "")}
            message = None
            for target in targets:
                target["event_ids"].append(event["id"])
            if kind == "agent.message":
                target = thread if thread is not None else current
                target["response_text"] += ("\n" if target["response_text"] else "") + text(event)
                message = {"role": "assistant", "content": text(event), **identity}
            elif kind in TOOL_USES:
                call = {"id": event["id"], "type": "function",
                        "function": {"name": event.get("name") or kind,
                                     "arguments": json.dumps(event.get("input") or event.get("arguments") or {})},
                        **identity}
                message = {"role": "assistant", "content": "", "tool_calls": [call], **identity}
                # A path read is evidence; mounting alone is not usage.
                used = _used_skill_names(
                    event.get("input") or event.get("arguments") or {}, names, skill_paths, event.get("name"),
                )
                for target in targets:
                    target["tool_calls"].append(call)
                    for name in used:
                        if name not in target["used_skills"]:
                            target["used_skills"].append(name)
                        target["skill_refs"].append({
                            "name": name, "source": "skill-tool" if event.get("name") == "skill" else "tool-path",
                            "used": True, **identity,
                        })
            elif kind in {"agent.tool_result", "agent.mcp_tool_result", "agent.custom_tool_result"}:
                call_id = event.get("tool_use_id") or event.get("custom_tool_use_id") or event.get("mcp_tool_use_id")
                call = next((c for c in current["tool_calls"] if c["id"] == call_id), {})
                output = {"tool_call_id": call_id, "tool_name": (call.get("function") or {}).get("name", ""),
                          "content": text(event), "has_error": event.get("is_error") is True
                          or event.get("has_error") is True, **identity}
                for target in targets:
                    target["tool_results"].append(output)
                    if output["has_error"]:
                        target["tool_errors"].append(output)
                message = {"role": "tool", **output}
            elif kind in THREAD_MESSAGES:
                # Native delegation is an event, not a fabricated function call.
                # Preserve direction and remote identity so judges can verify
                # that a child actually ran instead of relying on reply prose.
                native = {key: event[key] for key in (
                    "id", "type", "processed_at", "session_thread_id", "agent_name",
                    "to_session_thread_id", "to_agent_name", "from_session_thread_id", "from_agent_name",
                ) if key in event}
                message = {"role": "assistant", "content": text(event), "ma_event": native, **identity}
            elif kind == "user.message" and thread is not None:
                thread["prompt_text"] += ("\n" if thread["prompt_text"] else "") + text(event)
                message = {"role": "user", "content": text(event), **identity}
            if message is not None:
                for target in targets:
                    target["messages"].append(message)
    # Delegation instructions often appear only on the parent timeline.
    for turn in result:
        for thread in turn["agent_threads"]:
            if not thread["prompt_text"]:
                thread["prompt_text"] = "\n".join(
                    m["content"] for m in turn["messages"]
                    if (m.get("ma_event") or {}).get("to_session_thread_id") == thread["session_thread_id"]
                ) or turn["prompt_text"]
    return result
