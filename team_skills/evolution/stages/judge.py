"""Session-level judge helpers.

The standalone judge LLM stage was merged into the single ``analyze_session``
call (see ``stages/analyze.py``). Only the programmatic helpers survive here —
JSON/score parsing, weighted overall, artifact extraction, and the
defect/exemplary predicates — which ``analyze`` and the orchestrator reuse so
scoring stays byte-identical.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_DIMENSION_KEYS = (
    "task_completion",
    "response_quality",
    "efficiency",
    "tool_usage",
)
_WEIGHTS = {
    "task_completion": 0.55,
    "response_quality": 0.30,
    "efficiency": 0.05,
    "tool_usage": 0.10,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = str(text or "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    if not clean:
        return None
    try:
        obj = json.loads(clean)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    match = _JSON_BLOCK_RE.search(clean)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_score(value: Any) -> Optional[float]:
    if not _is_number(value) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return round(float(value), 3)


def _normalize_reason_list(value: Any) -> list[str]:
    """Coerce one dimension's reason payload into a list of bullet strings.

    Tolerates models that emit a single string, a multi-line string, or a
    list with empty/None entries instead of the required string array.
    """
    if isinstance(value, str):
        parts = [part.strip() for part in value.splitlines()]
        return [part for part in parts if part]
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            items.append(text)
    return items


def _parse_reasons(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Extract per-dimension scoring reasons from the judge payload.

    Accepts the canonical nested ``reasons`` object plus flat fallbacks
    (``<dim>_reasons`` / ``<dim>_reason``) so slightly-off-schema outputs
    still surface their reasons.
    """
    nested = payload.get("reasons") if isinstance(payload.get("reasons"), dict) else {}
    reasons: dict[str, list[str]] = {}
    for key in _DIMENSION_KEYS:
        items = _normalize_reason_list(nested.get(key))
        if not items:
            items = _normalize_reason_list(payload.get(f"{key}_reasons"))
        if not items:
            items = _normalize_reason_list(payload.get(f"{key}_reason"))
        if items:
            reasons[key] = items
    return reasons


def _parse_skill_experiences(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize LLM-authored Skill lessons without inventing missing facts."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("skill_experiences") or []:
        if not isinstance(raw, dict):
            continue
        skill_name = _clip_text(raw.get("skill_name"), 200)
        kind = str(raw.get("kind") or "").strip().lower()
        experience_key = _clip_text(raw.get("experience_key"), 160).lower()
        description = _clip_text(raw.get("description"), 1200)
        if (
            not skill_name
            or kind not in {"defect", "exemplary"}
            or not experience_key
            or not description
        ):
            continue
        identity = (skill_name, kind, experience_key)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "skill_name": skill_name,
                "kind": kind,
                "experience_key": experience_key,
                "description": description,
            }
        )
        if len(result) >= 8:
            break
    return result


def _clip_text(value: Any, max_chars: int = 8000) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _compute_weighted_overall(scores: dict[str, float]) -> float:
    total = 0.0
    for key in _DIMENSION_KEYS:
        total += scores[key] * _WEIGHTS[key]
    return round(total, 3)


def _build_judge_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "num_turns": session.get("num_turns"),
        "skills_referenced": sorted(session.get("_skills_referenced") or []),
        "has_tool_errors": bool(session.get("_has_tool_errors")),
        "prior_prm_scores": list(session.get("_prm_scores") or []),
        "avg_prm_before_judge": session.get("_avg_prm"),
        "source_artifacts": _extract_source_artifacts(session),
        "output_artifacts": _extract_output_artifacts(session),
        "trajectory": session.get("_trajectory") or "",
        "summary": session.get("_summary") or "",
    }


def _extract_output_artifacts(
    session: dict[str, Any],
    *,
    max_artifacts: int = 12,
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for tool_call in turn.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != "write":
                continue
            raw_args = function.get("arguments")
            if not isinstance(raw_args, str):
                continue
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                continue
            path = str(args.get("path") or "").strip()
            content = args.get("content")
            if not path or content is None:
                continue
            artifacts.append(
                {
                    "path": path,
                    "content": _clip_text(content),
                }
            )
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
        for tool_result in turn.get("tool_results") or []:
            if not isinstance(tool_result, dict):
                continue
            result = (
                tool_result.get("result")
                if isinstance(tool_result.get("result"), dict)
                else {}
            )
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            path = str(
                data.get("output_path")
                or data.get("artifact_path")
                or data.get("file_path")
                or ""
            ).strip()
            if not path or path in seen_paths:
                continue
            preview = json.dumps(data, ensure_ascii=False, default=str)
            artifact_path = Path(path).expanduser()
            try:
                if artifact_path.is_file():
                    preview = artifact_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
            except OSError:
                pass
            artifacts.append({"path": path, "content": _clip_text(preview)})
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
    return artifacts


def _extract_source_artifacts(
    session: dict[str, Any],
    *,
    max_artifacts: int = 12,
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue

        call_args_by_id: dict[str, dict[str, Any]] = {}
        for tool_call in turn.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != "read":
                continue
            raw_args = function.get("arguments")
            if not isinstance(raw_args, str):
                continue
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                continue
            call_id = str(tool_call.get("id") or "").replace("_", "")
            if call_id:
                call_args_by_id[call_id] = parsed_args

        for tool_result in turn.get("tool_results") or []:
            if not isinstance(tool_result, dict):
                continue
            if str(tool_result.get("tool_name") or "").strip() != "read":
                continue
            if bool(tool_result.get("has_error")):
                continue
            result_call_id = str(tool_result.get("tool_call_id") or "").replace("_", "")
            args = call_args_by_id.get(result_call_id)
            if not isinstance(args, dict):
                continue
            path = str(args.get("path") or "").strip()
            if not path or path in seen_paths:
                continue
            if path.startswith("/root/"):
                continue
            content = str(tool_result.get("content") or "").strip()
            if not content or content == "(see attached image)":
                continue
            artifacts.append(
                {
                    "path": path,
                    "content": _clip_text(content),
                }
            )
            seen_paths.add(path)
            if len(artifacts) >= max_artifacts:
                return artifacts
    return artifacts


def _apply_judge_scores(session: dict[str, Any], scores: dict[str, Any]) -> None:
    turns = session.get("turns") or []
    previous_prm_scores = list(session.get("_prm_scores") or [])
    previous_last_prm = None
    if turns and isinstance(turns[-1], dict):
        previous_last_prm = turns[-1].get("prm_score")
        turns[-1]["prm_score"] = scores["overall_score"]

    judge_scores = dict(scores)
    if previous_prm_scores:
        judge_scores["original_prm_scores"] = previous_prm_scores
    if previous_last_prm is not None:
        judge_scores["previous_last_prm_score"] = previous_last_prm

    session["_judge_scores"] = judge_scores
    session["_prm_scores"] = [scores["overall_score"]]
    session["_avg_prm"] = scores["overall_score"]


def _parse_scores(raw: str) -> Optional[dict[str, Any]]:
    payload = _extract_json_object(raw)
    if not payload:
        return None

    scores: dict[str, float] = {}
    for key in _DIMENSION_KEYS:
        normalized = _normalize_score(payload.get(key))
        if normalized is None:
            return None
        scores[key] = normalized

    overall = _compute_weighted_overall(scores)
    result = {
        **scores,
        "overall_score": overall,
        "rationale": str(payload.get("rationale") or "").strip(),
    }
    reasons = _parse_reasons(payload)
    if reasons:
        result["reasons"] = reasons
    raw_overall = _normalize_score(payload.get("overall_score"))
    if raw_overall is not None:
        result["model_overall_score"] = raw_overall
    evidence = str(payload.get("evolution_evidence") or "").strip().lower()
    result["evolution_evidence"] = evidence if evidence in {"defect", "exemplary", "none"} else "none"
    if result["evolution_evidence"] != "none":
        evidence_reason = str(payload.get("evidence_reason") or "").strip()
        if evidence_reason:
            result["evidence_reason"] = evidence_reason
    skill_experiences = _parse_skill_experiences(payload)
    if "skill_experiences" in payload:
        result["skill_experiences"] = skill_experiences
    return result


_DEFECT_SCORE_THRESHOLD_DEFAULT = 0.5


def _defect_threshold() -> float:
    try:
        return max(
            0.0,
            min(
                1.0,
                float(
                    os.environ.get(
                        "EVOLVE_REQUEUE_DEFECT_THRESHOLD",
                        str(_DEFECT_SCORE_THRESHOLD_DEFAULT),
                    )
                ),
            ),
        )
    except ValueError:
        return _DEFECT_SCORE_THRESHOLD_DEFAULT


def judge_has_defect_evidence(scores: Any, threshold: Optional[float] = None) -> bool:
    """True when a judge result carries reusable defect evidence.

    Either the overall score falls below the requeue threshold (a failing
    session likely exposing a skill/process gap) or the judge explicitly
    flagged ``evolution_evidence == "defect"``.
    """
    if not isinstance(scores, dict):
        return False
    if threshold is None:
        threshold = _defect_threshold()
    overall = scores.get("overall_score")
    if (
        isinstance(overall, (int, float))
        and not isinstance(overall, bool)
        and float(overall) < threshold
    ):
        return True
    return str(scores.get("evolution_evidence") or "").strip().lower() == "defect"


def judge_is_exemplary(scores: Any) -> bool:
    """True when the judge flagged the session as an exemplary goodcase."""
    if not isinstance(scores, dict):
        return False
    return str(scores.get("evolution_evidence") or "").strip().lower() == "exemplary"
