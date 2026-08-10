"""Persistent cross-cycle evidence for skill evolution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ...storage import is_not_found_error

_SCHEMA_VERSION = 1
_SUMMARY_LIMIT = 4_000
_TRAJECTORY_LIMIT = 6_000
_RESPONSE_LIMIT = 4_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 16].rstrip() + "\n...[truncated]"


def _safe_slug(value: str) -> str:
    raw = str(value or "no-skill").strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-") or "no-skill"
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]
    return f"{slug[:80]}-{digest}"


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def is_candidate_audit_session(session: dict[str, Any]) -> bool:
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    return (
        str(session.get("source") or "").strip()
        == "managed_agent_candidate_audit"
        or bool(str(runtime_context.get("candidate_job_id") or "").strip())
    )


def _extract_replay_cases(session: dict[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    if is_candidate_audit_session(session):
        return []
    preferred: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    session_id = str(session.get("session_id") or "")
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    evaluation_profile = str(
        runtime_context.get("evaluation_profile") or ""
    ).strip()
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        instruction = str(turn.get("prompt_text") or turn.get("instruction") or "").strip()
        response = str(turn.get("response_text") or turn.get("response") or "").strip()
        if not instruction:
            continue
        case = {
            "session_id": session_id,
            "turn_num": int(turn.get("turn_num") or 0),
            "instruction": _clip(instruction, 3_000),
            "reference_response": _clip(response, _RESPONSE_LIMIT),
            "had_tool_calls": bool(turn.get("tool_calls")),
            "had_tool_results": bool(
                turn.get("tool_results") or turn.get("tool_observations")
            ),
        }
        if evaluation_profile:
            case["evaluation_profile"] = evaluation_profile
        target = preferred if int(turn.get("turn_num") or 0) <= 1 else fallback
        target.append(case)
    return (preferred + fallback)[: max(0, int(limit))]


def _entry_from_session(session: dict[str, Any]) -> dict[str, Any]:
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    judge_scores = (
        session.get("_judge_scores")
        if isinstance(session.get("_judge_scores"), dict)
        else {}
    )
    overall = judge_scores.get("overall_score")
    if not isinstance(overall, (int, float)) or isinstance(overall, bool):
        overall = None
    avg_prm = session.get("_avg_prm")
    if not isinstance(avg_prm, (int, float)) or isinstance(avg_prm, bool):
        avg_prm = None
    observed_at = str(
        session.get("ingested_at")
        or session.get("timestamp")
        or session.get("started_at")
        or _utc_now_iso()
    )
    return {
        "session_id": str(session.get("session_id") or "").strip(),
        "observed_at": observed_at,
        "captured_at": _utc_now_iso(),
        "summary": _clip(session.get("_summary"), _SUMMARY_LIMIT),
        "trajectory": _clip(session.get("_trajectory"), _TRAJECTORY_LIMIT),
        "judge_overall_score": overall,
        "avg_prm": avg_prm,
        "has_tool_errors": bool(session.get("_has_tool_errors")),
        "source": str(session.get("source") or ""),
        "evaluation_profile": str(
            runtime_context.get("evaluation_profile") or ""
        ).strip(),
        "replay_cases": _extract_replay_cases(session),
    }


def _stratified_history(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    limit = max(0, int(limit))
    if limit <= 0 or not entries:
        return []
    if len(entries) <= limit:
        return list(entries)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    failures = [
        item
        for item in entries
        if item.get("has_tool_errors")
        or (
            isinstance(item.get("judge_overall_score"), (int, float))
            and float(item["judge_overall_score"]) < 0.6
        )
    ]
    for item in failures[-max(1, limit // 2) :]:
        session_id = str(item.get("session_id") or "")
        if session_id not in selected_ids:
            selected.append(item)
            selected_ids.add(session_id)
        if len(selected) >= limit:
            return sorted(selected, key=lambda row: str(row.get("observed_at") or ""))

    remaining = [item for item in entries if str(item.get("session_id") or "") not in selected_ids]
    slots = limit - len(selected)
    if slots > 0 and remaining:
        if slots == 1:
            indices = [len(remaining) // 2]
        else:
            indices = [
                round(index * (len(remaining) - 1) / (slots - 1))
                for index in range(slots)
            ]
        for index in indices:
            item = remaining[index]
            session_id = str(item.get("session_id") or "")
            if session_id not in selected_ids:
                selected.append(item)
                selected_ids.add(session_id)
    return sorted(selected[:limit], key=lambda row: str(row.get("observed_at") or ""))


class SkillEvidenceStore:
    """Object-store backed evidence ledger keyed by skill name."""

    def __init__(
        self,
        bucket: Any,
        *,
        prefix: str = "",
        max_entries: int = 200,
        recent_limit: int = 12,
        historical_limit: int = 12,
        replay_cases_per_window: int = 1,
        change_debt_threshold: int = 3,
    ) -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")
        self.max_entries = max(1, int(max_entries))
        self.recent_limit = max(1, int(recent_limit))
        self.historical_limit = max(0, int(historical_limit))
        self.replay_cases_per_window = max(1, int(replay_cases_per_window))
        self.change_debt_threshold = max(1, int(change_debt_threshold))

    def _key(self, skill_name: str) -> str:
        return f"{self._prefix}skill_evidence/{_safe_slug(skill_name)}.json"

    @staticmethod
    def _empty_state(skill_name: str) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "skill_name": str(skill_name or ""),
            "updated_at": "",
            "evidence": [],
            "change_debt": {
                "skip_count": 0,
                "pending_session_ids": [],
                "rationales": [],
                "reconsideration_ready": False,
            },
            "active_candidate_job_id": "",
            "last_published_at": "",
            "last_published_job_id": "",
            "published_evidence_session_ids": [],
        }

    def load(self, skill_name: str) -> dict[str, Any]:
        try:
            payload = json.loads(
                self._bucket.get_object(self._key(skill_name)).read().decode("utf-8")
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                raise
            return self._empty_state(skill_name)
        if not isinstance(payload, dict):
            return self._empty_state(skill_name)
        state = self._empty_state(skill_name)
        state.update(payload)
        if not isinstance(state.get("evidence"), list):
            state["evidence"] = []
        if not isinstance(state.get("change_debt"), dict):
            state["change_debt"] = self._empty_state(skill_name)["change_debt"]
        return state

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        state["schema_version"] = _SCHEMA_VERSION
        state["updated_at"] = _utc_now_iso()
        self._bucket.put_object(
            self._key(str(state.get("skill_name") or "")),
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return state

    def record_sessions(
        self,
        skill_name: str,
        sessions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        state = self.load(skill_name)
        entries = {
            str(item.get("session_id") or ""): item
            for item in state.get("evidence") or []
            if isinstance(item, dict) and str(item.get("session_id") or "")
        }
        for session in sessions:
            if not isinstance(session, dict) or session.get("_evidence_window"):
                continue
            entry = _entry_from_session(session)
            session_id = entry["session_id"]
            if not session_id:
                continue
            previous = entries.get(session_id)
            if previous:
                entry["first_captured_at"] = str(
                    previous.get("first_captured_at")
                    or previous.get("captured_at")
                    or entry["captured_at"]
                )
            else:
                entry["first_captured_at"] = entry["captured_at"]
            entries[session_id] = entry
        ordered = sorted(
            entries.values(),
            key=lambda item: (
                str(item.get("observed_at") or ""),
                str(item.get("captured_at") or ""),
                str(item.get("session_id") or ""),
            ),
        )
        state["evidence"] = ordered[-self.max_entries :]
        return self._save(state)

    def record_skip(
        self,
        skill_name: str,
        sessions: list[dict[str, Any]],
        rationale: str,
    ) -> dict[str, Any]:
        state = self.record_sessions(skill_name, sessions)
        debt = (
            dict(state.get("change_debt") or {})
            if isinstance(state.get("change_debt"), dict)
            else {}
        )
        pending = _unique_strings(
            list(debt.get("pending_session_ids") or [])
            + [session.get("session_id") for session in sessions]
        )
        rationales = [
            item
            for item in debt.get("rationales") or []
            if isinstance(item, dict)
        ]
        if str(rationale or "").strip():
            rationales.append(
                {
                    "at": _utc_now_iso(),
                    "reason": _clip(rationale, 1_000),
                    "session_ids": _unique_strings(
                        [session.get("session_id") for session in sessions]
                    ),
                }
            )
        debt.update(
            {
                "skip_count": int(debt.get("skip_count") or 0) + 1,
                "pending_session_ids": pending,
                "rationales": rationales[-20:],
                "reconsideration_ready": len(pending) >= self.change_debt_threshold,
                "threshold": self.change_debt_threshold,
            }
        )
        state["change_debt"] = debt
        return self._save(state)

    def record_candidate(
        self,
        skill_name: str,
        sessions: list[dict[str, Any]],
        job_id: str,
    ) -> dict[str, Any]:
        state = self.record_sessions(skill_name, sessions)
        state["active_candidate_job_id"] = str(job_id or "")
        return self._save(state)

    def mark_published(self, skill_name: str, job_id: str = "") -> dict[str, Any]:
        state = self.load(skill_name)
        state["change_debt"] = self._empty_state(skill_name)["change_debt"]
        state["active_candidate_job_id"] = ""
        state["last_published_at"] = _utc_now_iso()
        state["last_published_job_id"] = str(job_id or "")
        state["published_evidence_session_ids"] = _unique_strings(
            [
                item.get("session_id")
                for item in state.get("evidence") or []
                if isinstance(item, dict)
            ]
        )
        return self._save(state)

    def _windows(
        self,
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        entries = [
            item
            for item in state.get("evidence") or []
            if isinstance(item, dict)
        ]
        published_ids = {
            str(item or "")
            for item in state.get("published_evidence_session_ids") or []
            if str(item or "")
        }
        if published_ids:
            unresolved = [
                item
                for item in entries
                if str(item.get("session_id") or "") not in published_ids
            ]
            recent = unresolved[-self.recent_limit :]
        else:
            recent = entries[-self.recent_limit :]
        recent_ids = {str(item.get("session_id") or "") for item in recent}
        historical_pool = [
            item
            for item in entries
            if str(item.get("session_id") or "") not in recent_ids
        ]
        historical = _stratified_history(historical_pool, self.historical_limit)
        return recent, historical

    @staticmethod
    def _synthetic_session(
        entry: dict[str, Any],
        window: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_id": str(entry.get("session_id") or ""),
            "_summary": str(entry.get("summary") or ""),
            "_trajectory": str(entry.get("trajectory") or ""),
            "_avg_prm": entry.get("avg_prm"),
            "_has_tool_errors": bool(entry.get("has_tool_errors")),
            "_evidence_window": window,
            "timestamp": str(entry.get("observed_at") or ""),
        }
        replay_profiles = [
            str(item.get("evaluation_profile") or "").strip()
            for item in entry.get("replay_cases") or []
            if isinstance(item, dict)
            and str(item.get("evaluation_profile") or "").strip()
        ]
        evaluation_profile = str(
            entry.get("evaluation_profile")
            or (replay_profiles[0] if replay_profiles else "")
        ).strip()
        if evaluation_profile:
            result["runtime_context"] = {
                "evaluation_profile": evaluation_profile,
            }
        overall = entry.get("judge_overall_score")
        if isinstance(overall, (int, float)) and not isinstance(overall, bool):
            result["_judge_scores"] = {"overall_score": float(overall)}
        return result

    def build_context(
        self,
        skill_name: str,
        current_sessions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        state = self.load(skill_name)
        recent, historical = self._windows(state)
        current_ids = {
            str(session.get("session_id") or "")
            for session in current_sessions
            if str(session.get("session_id") or "")
        }
        planning_sessions = list(current_sessions)
        for window, entries in (("recent", recent), ("historical", historical)):
            for entry in entries:
                if str(entry.get("session_id") or "") in current_ids:
                    continue
                planning_sessions.append(self._synthetic_session(entry, window))

        all_entries = [
            item
            for item in state.get("evidence") or []
            if isinstance(item, dict)
        ]
        scores = [
            float(item["judge_overall_score"])
            for item in all_entries
            if isinstance(item.get("judge_overall_score"), (int, float))
            and not isinstance(item.get("judge_overall_score"), bool)
        ]
        debt = dict(state.get("change_debt") or {})
        context = {
            "skill_name": skill_name,
            "total_evidence_sessions": len(all_entries),
            "current_session_count": len(current_sessions),
            "recent_session_ids": [
                str(item.get("session_id") or "") for item in recent
            ],
            "historical_session_ids": [
                str(item.get("session_id") or "") for item in historical
            ],
            "tool_error_sessions": sum(
                1 for item in all_entries if item.get("has_tool_errors")
            ),
            "mean_judge_score": (
                round(sum(scores) / len(scores), 3) if scores else None
            ),
            "change_debt": debt,
            "active_candidate_job_id": str(
                state.get("active_candidate_job_id") or ""
            ),
        }
        return planning_sessions, context

    def build_replay_windows(self, skill_name: str) -> dict[str, list[dict[str, Any]]]:
        state = self.load(skill_name)
        recent, historical = self._windows(state)

        def collect(
            entries: list[dict[str, Any]],
            window: str,
        ) -> list[dict[str, Any]]:
            cases: list[dict[str, Any]] = []
            seen: set[tuple[str, int, str]] = set()
            for entry in reversed(entries):
                for raw in entry.get("replay_cases") or []:
                    if not isinstance(raw, dict):
                        continue
                    case = dict(raw)
                    key = (
                        str(case.get("session_id") or ""),
                        int(case.get("turn_num") or 0),
                        str(case.get("instruction") or ""),
                    )
                    if not key[2] or key in seen:
                        continue
                    case["evidence_window"] = window
                    cases.append(case)
                    seen.add(key)
                    if len(cases) >= self.replay_cases_per_window:
                        return cases
            return cases

        return {
            "recent": collect(recent, "recent"),
            "historical": collect(historical, "historical"),
        }
