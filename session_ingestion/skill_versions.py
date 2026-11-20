"""Historical Skill references and call evidence, without consulting live versions."""
from collections.abc import Mapping


def _object(value):
    return value if isinstance(value, Mapping) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _name(value):
    return _text(value) if isinstance(value, str) else _text(
        _object(value).get("name") or _object(value).get("skill_name"))


def _version(value):
    if isinstance(value, bool):
        return None
    raw = str(value) if isinstance(value, int) else _text(value)
    return raw if raw.isascii() and raw.isdigit() and raw.strip("0") else None


def snapshot_skill_ids(session):
    graph = _object(_object(session.get("runtime")).get("agent_graph"))
    return sorted({
        _text(ref.get("skill_id"))
        for agent in _object(graph.get("agents")).values()
        for ref in _items(_object(agent).get("skills"))
        if isinstance(ref, Mapping) and _text(ref.get("skill_id"))
    })


def session_skill_versions(session, skill_names=None, *, meta=None):
    """Project the archived graph and evidence; ambiguous callers stay explicit."""
    names = skill_names or {}
    graph = _object(_object(session.get("runtime")).get("agent_graph"))
    agents = _object(graph.get("agents"))
    root = _text(graph.get("entry_agent_id"))
    rows, keys = [], {}
    for ident, raw_agent in agents.items():
        agent = _object(raw_agent)
        for ref in _items(agent.get("skills")):
            ref = _object(ref)
            sid = _text(ref.get("skill_id"))
            if not sid:
                continue
            version = _version(ref.get("version"))
            key = (str(ident), sid, version)
            if key in keys:
                continue
            row = {
                "name": _name(ref) or _text(names.get(sid)) or sid,
                "skill_id": sid, "version": version,
                "agent_id": str(ident), "agent_name": _text(agent.get("name")) or str(ident),
                "agent_role": "main" if str(ident) == root else "member" if root else "unknown",
                "agent_version": _version(agent.get("version")),
                "usage": "configured",
                "unknown_reason": "" if version else "会话引用未记录固定版本",
                "event_ids": [],
            }
            keys[key] = row
            rows.append(row)

    contexts = []
    for raw_turn in _items(session.get("turns")):
        turn = _object(raw_turn)
        contexts.append((turn, {}))
        contexts.extend((_object(thread), _object(thread)) for thread in _items(turn.get("agent_threads")))
    evidence, aggregates = {}, []
    direct_names = set()
    for turn, context in contexts:
        for raw_ref in _items(turn.get("skill_refs")):
            ref = _object(raw_ref)
            if ref.get("source") == "configured":
                continue
            if ref.get("used") is not True and ref.get("source") not in {"skill-tool", "tool-path"}:
                continue
            name, sid = _name(ref), _text(ref.get("skill_id"))
            if not name and not sid:
                continue
            event_id = _text(ref.get("event_id"))
            thread_id = _text(ref.get("session_thread_id") or context.get("session_thread_id"))
            agent_id = _text(ref.get("agent_id") or context.get("agent_id"))
            agent_name = _text(ref.get("agent_name") or context.get("agent_name"))
            key = (event_id, name, sid, thread_id)
            value = {"name": name, "skill_id": sid, "agent_id": agent_id,
                     "agent_name": agent_name, "event_id": event_id, "thread_id": thread_id}
            prior = evidence.get(key)
            # Parent projections repeat child calls; retain the strongest
            # archived caller identity rather than counting the call twice.
            if prior is None or (bool(agent_id), bool(agent_name)) > (
                    bool(prior["agent_id"]), bool(prior["agent_name"])):
                evidence[key] = value
            if name:
                direct_names.add(name)
        for field in ("used_skills", "read_skills", "modified_skills"):
            aggregates.extend({
                "name": _name(value), "skill_id": "", "agent_id": _text(context.get("agent_id")),
                "agent_name": _text(context.get("agent_name")), "event_id": "", "thread_id": "",
            } for value in _items(turn.get(field)) if _name(value))
    for value in [*_items(session.get("used_skills")), *_items(_object(meta).get("used_skills"))]:
        if _name(value):
            aggregates.append({"name": _name(value), "skill_id": "", "agent_id": "", "agent_name": "",
                               "event_id": "", "thread_id": ""})
    calls = [*evidence.values(), *(value for value in aggregates if value["name"] not in direct_names)]
    unknown = {}
    for call in calls:
        ambiguous_agent = False
        candidates = [row for row in rows if row["skill_id"] == call["skill_id"]] if call["skill_id"] else [
            row for row in rows if row["name"] == call["name"]]
        if call["agent_id"]:
            candidates = [row for row in candidates if row["agent_id"] == call["agent_id"]]
        elif call["agent_name"]:
            matches = [ident for ident, agent in agents.items()
                       if (_text(_object(agent).get("name")) or str(ident)) == call["agent_name"]]
            ambiguous_agent = len(matches) > 1
            candidates = [row for row in candidates if row["agent_name"] == call["agent_name"]]
            if ambiguous_agent:
                candidates = []
        if len(candidates) == 1:
            row = candidates[0]
            row["usage"] = "used"
            if call["event_id"] and call["event_id"] not in row["event_ids"]:
                row["event_ids"].append(call["event_id"])
            continue
        key = (call["name"], call["skill_id"], call["agent_id"], call["agent_name"])
        if key not in unknown:
            unknown[key] = {
                "name": call["name"] or call["skill_id"], "skill_id": call["skill_id"] or None,
                "version": None, "agent_id": call["agent_id"] or None,
                "agent_name": call["agent_name"] or "调用 Agent 未确认", "agent_role": "unknown",
                "agent_version": None, "usage": "used",
                "unknown_reason": "存在多个同名 Agent，调用归属无法确认" if ambiguous_agent else
                                  "调用记录无法唯一对应会话中的 Skill 引用" if candidates else
                                  "会话快照中缺少对应的 Skill 引用",
                "event_ids": [],
            }
        if call["event_id"] and call["event_id"] not in unknown[key]["event_ids"]:
            unknown[key]["event_ids"].append(call["event_id"])
    return [*rows, *unknown.values()]
