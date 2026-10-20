"""Model-based semantic grouping with deterministic, grounded references."""
from __future__ import annotations

import json
import re

from team_skills.evolution.repository import canonical, digest


def source_fingerprint(experiences):
    return digest(sorted(experiences, key=lambda e: e["id"]))


def normalize_patterns(skill, raw_patterns, experiences, previous):
    by_id = {e["id"]: e for e in experiences}
    output, used = [], set()
    for raw in raw_patterns[:200]:
        if not isinstance(raw, dict):
            continue
        raw_ids = raw.get("experience_ids")
        if not isinstance(raw_ids, list):
            continue
        ids = sorted({i for i in raw_ids if isinstance(i, str) and i in by_id})
        kinds = {by_id[i]["kind"] for i in ids}
        if len(kinds) != 1:
            continue
        kind = next(iter(kinds))
        key = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("pattern_key") or "").lower()).strip("_")[:100]
        key = key or "pattern_" + digest(ids)[:16]
        candidates = [p for p in previous if p["kind"] == kind and p["pattern_id"] not in used
                      and (p["pattern_key"] == key or set(ids).intersection(p["experience_ids"]))]
        prior = max(candidates, key=lambda p: (p["pattern_key"] == key,
                    len(set(ids).intersection(p["experience_ids"]))), default=None)
        identity = prior["pattern_id"] if prior else "pat_" + digest([skill, kind, key])[:24]
        if identity in used:
            identity = "pat_" + digest([skill, kind, key, ids])[:24]
        if identity in used:
            continue
        used.add(identity)
        sessions = sorted({s for i in ids for s in by_id[i]["session_ids"]})
        users = sorted({u for i in ids for u in by_id[i]["user_aliases"]})
        ready = len(sessions) >= 2 and len(users) >= 2
        output.append({
            "pattern_id": identity, "pattern_key": key, "skill_name": skill, "kind": kind,
            "title": str(raw.get("title") or key)[:240], "problem": str(raw.get("problem") or "")[:4000],
            "root_cause": str(raw.get("root_cause") or "")[:4000],
            "guidance": str(raw.get("guidance") or "")[:6000],
            "experience_ids": ids, "session_ids": sessions, "user_aliases": users,
            "evidence_count": len(ids), "occurrence_count": sum(by_id[i]["occurrence_count"] for i in ids),
            "session_count": len(sessions), "user_count": len(users), "team_ready": ready,
            "status": "ready" if ready else "observing",
        })
    if not output:
        raise ValueError("pattern compiler produced no grounded patterns")
    return output


class PatternCompiler:
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, *, skill, experiences, previous):
        response = await self.llm.chat(
            [
                {"role": "system", "content": (
                    "你是 Skill 经验模式归纳器。将语义等价的经验合并为可执行模式；"
                    "错误经验和优秀实践不要混合。只能引用输入中的 id，不得补造证据。"
                    "输出严格 JSON：{\"patterns\":[{\"pattern_key\":\"stable_snake_case\","
                    "\"title\":\"...\",\"problem\":\"...\",\"root_cause\":\"...\","
                    "\"guidance\":\"...\",\"experience_ids\":[\"...\"]}]}。")},
                {"role": "user", "content": canonical({"skill_name": skill, "experiences": experiences})},
            ],
            temperature=0.1, max_tokens=12000, trace_name="skill_experience_pattern.compile",
            trace_metadata={"component": "team_skills.experiences", "skill_name": skill},
        )
        clean = re.sub(r"<think>.*?</think>", "", str(response), flags=re.DOTALL)
        clean = re.sub(r"```(?:json)?\s*", "", clean).strip().rstrip("`").strip()
        value = json.loads(clean)
        if not isinstance(value, dict) or not isinstance(value.get("patterns"), list):
            raise ValueError("pattern compiler response requires patterns")
        # Return raw groups: the handler always grounds them, including output
        # from an injected compiler used by a different model integration.
        return value["patterns"]


def compact_context(patterns, limit=8):
    ready = sorted((p for p in patterns if p["team_ready"]),
                   key=lambda p: p["occurrence_count"], reverse=True)[:limit]
    context = [{k: v for k, v in p.items() if k not in {"candidate_outcomes", "user_aliases"}} for p in ready]
    refs = [{k: p[k] for k in ("skill_name", "pattern_id", "pattern_key")} for p in ready]
    return context, refs
