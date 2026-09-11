"""Turn parsing and decision validation for the skill-evolution agent loop.

The loop speaks a text JSON protocol (no native tool calling): every
assistant turn is one JSON object — a plan (round 1), a batch of tool
calls, or a final decision. This module owns:

* ``parse_turn`` — lenient extraction of that JSON object from raw model
  output (reasoning-block strip, outer-fence strip, bracket-balanced
  extraction, optional json_repair for the last round only) plus turn-type
  detection (explicit ``type`` field, structural fields, or legacy
  one-shot shapes kept for migration compatibility).
* ``normalize_classification`` — the five evidence buckets shared by plan
  and final turns.
* ``validate_final`` — decision validation returning per-field errors so
  the runner can feed them back as a correction round instead of silently
  degrading.
* ``legacy_suppress`` — the old one-shot normalization semantics, used as
  the exhaustion fallback (no team-skill evidence suppresses to skip, a
  create reusing the current name is coerced to improve, ...).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..kernel.enums import DecisionAction

logger = logging.getLogger(__name__)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_LEADING_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\n?")
_TRAILING_FENCE_RE = re.compile(r"\n?```[ \t]*$")

_CLASSIFICATION_BUCKETS = (
    "team_skill",
    "user_memory",
    "task_requirement",
    "agent_runtime",
    "insufficient_evidence",
)


def _strip_reasoning(text: str) -> str:
    return _THINK_BLOCK_RE.sub("", str(text or "").strip())


def _strip_outer_fences(text: str) -> str:
    """Strip only a leading/trailing code fence — never fences inside strings."""
    out = str(text or "").strip()
    out = _LEADING_FENCE_RE.sub("", out, count=1)
    out = _TRAILING_FENCE_RE.sub("", out)
    return out.strip()


def extract_json_object(text: str) -> Optional[str]:
    """Bracket-balanced extraction of the first complete JSON object."""
    start = text.find("{")
    if start == -1:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}
    for index in range(start, len(text)):
        char = text[index]
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
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def _loads_lenient(clean: str, *, allow_repair: bool) -> Optional[Any]:
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        pass
    extracted = extract_json_object(clean)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except (json.JSONDecodeError, ValueError):
            pass
    if allow_repair:
        try:
            from json_repair import repair_json

            repaired = repair_json(clean, return_objects=True)
            if isinstance(repaired, dict):
                return repaired
        except Exception:  # noqa: BLE001 - repair is best-effort only
            return None
    return None


def parse_turn(raw: str, *, allow_repair: bool = False) -> dict[str, Any]:
    """Parse one assistant turn of the loop protocol.

    Returns ``{"ok": True, "type": ..., ...}`` with one of ``plan`` /
    ``calls`` / ``decision`` depending on the detected type, or
    ``{"ok": False, "error": ...}``. Detection order: explicit ``type``
    field, then structural fields, then legacy one-shot shapes (an
    evolve/create decision object, or a bare merge skill object).
    """
    text = _strip_outer_fences(_strip_reasoning(raw))
    if not text:
        return {"ok": False, "error": "empty turn"}
    obj = _loads_lenient(text, allow_repair=allow_repair)
    if not isinstance(obj, dict):
        return {"ok": False, "error": "turn is not a JSON object"}

    turn_type = str(obj.get("type") or "").strip().lower()
    if turn_type == "act":
        turn_type = "tool_calls"
    if turn_type not in {"plan", "tool_calls", "final"}:
        if isinstance(obj.get("calls"), list) or isinstance(obj.get("tool_calls"), list):
            turn_type = "tool_calls"
        elif isinstance(obj.get("final"), (dict, str)) or isinstance(obj.get("decision"), dict):
            turn_type = "final"
        elif isinstance(obj.get("action"), str):
            # Legacy one-shot decision shape (evolve/create).
            turn_type = "final"
        elif isinstance(obj.get("action_candidate"), str) or isinstance(obj.get("plan"), list):
            turn_type = "plan"
        elif obj.get("name") and obj.get("content"):
            # Legacy merge output: a bare skill object.
            turn_type = "final"
        else:
            return {"ok": False, "error": "unrecognized turn shape"}

    if turn_type == "plan":
        return {"ok": True, "type": "plan", "plan": obj}

    if turn_type == "tool_calls":
        calls = obj.get("calls") if isinstance(obj.get("calls"), list) else obj.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return {"ok": False, "error": "tool_calls 必须是非空列表"}
        return {"ok": True, "type": "tool_calls", "calls": calls}

    # final: locate the decision payload.
    decision: Any = None
    if isinstance(obj.get("final"), dict):
        decision = obj["final"]
    elif isinstance(obj.get("final"), str):
        decision = _loads_lenient(str(obj["final"]), allow_repair=allow_repair)
    elif isinstance(obj.get("decision"), dict):
        decision = obj["decision"]
    if not isinstance(decision, dict):
        decision = obj
    return {"ok": True, "type": "final", "decision": decision}


def normalize_classification(raw: Any) -> dict[str, list]:
    """Normalize the five evidence buckets; missing buckets become lists."""
    source = raw if isinstance(raw, dict) else {}
    classification: dict[str, list] = {}
    for bucket in _CLASSIFICATION_BUCKETS:
        values = source.get(bucket)
        classification[bucket] = values if isinstance(values, list) else []
    return classification


def validate_final(
    decision: dict,
    skill_name: str,
    *,
    stage: str,
) -> tuple[Optional[dict], list[str]]:
    """Validate a final decision; return ``(normalized, errors)``.

    ``normalized`` is None when validation failed; ``errors`` are
    human-readable strings the runner feeds back as one correction round.
    """
    if stage == "merge":
        skill = decision.get("skill") if isinstance(decision.get("skill"), dict) else decision
        if not isinstance(skill, dict) or not str(skill.get("name") or "").strip():
            return None, ["合并结果缺少 name"]
        if not str(skill.get("content") or "").strip():
            return None, ["合并结果缺少 content（合并后的 Markdown 正文）"]
        return skill, []

    action = str(decision.get("action") or "").strip()
    if action == DecisionAction.SKIP:
        return {
            "action": DecisionAction.SKIP,
            "rationale": str(decision.get("rationale") or ""),
            "evidence_classification": normalize_classification(
                decision.get("evidence_classification")
            ),
        }, []

    classification = normalize_classification(decision.get("evidence_classification"))
    if action not in {
        DecisionAction.IMPROVE,
        DecisionAction.OPTIMIZE_DESC,
        DecisionAction.CREATE,
    }:
        return None, [
            "未知 action："
            f"{action!r}（必须是 improve_skill / optimize_description / create_skill / skip）"
        ]
    if not classification["team_skill"]:
        return None, [
            "evidence_classification.team_skill 为空：只有存在可复用 team_skill 证据时"
            "才能修改共享 Skill；否则应提交 skip"
        ]

    skill_data = decision.get("skill")
    if not isinstance(skill_data, dict):
        return None, [f"action 为 {action} 但缺少 skill 对象"]

    if action == DecisionAction.CREATE and not str(skill_data.get("name") or "").strip():
        return None, ["create_skill 缺少新 skill 的 name"]

    if (
        action == DecisionAction.IMPROVE
        and isinstance(skill_data.get("edits"), list)
        and skill_data["edits"]
    ):
        return None, [
            "improve_skill 的修改必须先经 propose_edits 工具暂存；"
            "final 中不要携带 edits 列表（省略 content 使用暂存结果，或提供完整 content）"
        ]

    return {
        "action": action,
        "rationale": str(decision.get("rationale") or ""),
        "skill": skill_data,
        "evidence_classification": classification,
    }, []


def legacy_suppress(decision: Any, skill_name: str) -> Optional[dict]:
    """Old one-shot normalization semantics, used at round exhaustion."""
    if not isinstance(decision, dict):
        return None
    action = str(decision.get("action") or DecisionAction.SKIP)
    classification = normalize_classification(decision.get("evidence_classification"))
    if action == DecisionAction.SKIP:
        return {
            "action": DecisionAction.SKIP,
            "rationale": str(decision.get("rationale") or ""),
            "evidence_classification": classification,
        }
    if not classification["team_skill"]:
        return {
            "action": DecisionAction.SKIP,
            "rationale": (
                "Candidate suppressed because its evidence classification contains "
                "no reusable team-skill evidence. "
                + str(decision.get("rationale") or "")
            ).strip(),
            "evidence_classification": classification,
        }

    skill_data = decision.get("skill")
    if not isinstance(skill_data, dict):
        return None
    if action == DecisionAction.CREATE:
        if not skill_data.get("name"):
            return None
        if skill_name and skill_data["name"] == skill_name:
            action = DecisionAction.IMPROVE
    elif skill_name and not skill_data.get("name"):
        skill_data["name"] = skill_name
    return {
        "action": action,
        "rationale": str(decision.get("rationale") or ""),
        "skill": skill_data,
        "evidence_classification": classification,
    }
