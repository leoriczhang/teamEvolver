"""Optional background validator for idle clients.

This worker is intentionally conservative:
- it is disabled by default
- it only runs when sharing is enabled
- it only picks up jobs when the local client appears idle
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from ..llm import AsyncLLMClient
from ..skills.render import build_skill_md
from .store import ValidationStore

logger = logging.getLogger(__name__)


class IdleStateProvider(Protocol):
    def active_session_count(self) -> int: ...
    def last_request_age_seconds(self) -> Optional[float]: ...
    def is_idle_for_validation(self, idle_after_seconds: int) -> bool: ...


@dataclass
class ValidationRunSummary:
    checked_jobs: int = 0
    validated_jobs: int = 0
    skipped_jobs: int = 0
    reason: str = ""


class ValidationWorker:
    """Idle-time client-side validator."""

    def __init__(
        self,
        config,
        *,
        idle_provider: Optional[IdleStateProvider] = None,
        llm_client: Any = None,
    ) -> None:
        self.config = config
        self._idle_provider = idle_provider
        self._store = ValidationStore.from_config(config)
        self._stop_event = asyncio.Event()
        self._jobs_completed_today = 0
        self._jobs_completed_date = datetime.now(timezone.utc).date().isoformat()
        self._client = llm_client or AsyncLLMClient(
            api_key=config.llm_api_key,
            base_url=config.llm_api_base,
            model=config.llm_model_id or config.model_name or "doubao-seed-evolving",
            max_tokens=4096,
            temperature=0.1,
        )
        self._user_alias = str(config.sharing_user_alias or os.environ.get("USER", "anonymous"))

    def stop(self) -> None:
        self._stop_event.set()

    def _validation_enabled(self) -> bool:
        return bool(self.config.validation_enabled and self.config.sharing_enabled)

    def _reset_daily_quota_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._jobs_completed_date:
            self._jobs_completed_date = today
            self._jobs_completed_today = 0

    def _quota_available(self) -> bool:
        self._reset_daily_quota_if_needed()
        limit = max(0, int(self.config.validation_max_jobs_per_day))
        return limit <= 0 or self._jobs_completed_today < limit

    def _is_idle(self, *, force: bool = False) -> bool:
        if force:
            return True
        if self._idle_provider is None:
            return False
        return bool(
            self._idle_provider.is_idle_for_validation(
                int(self.config.validation_idle_after_seconds),
            )
        )

    @staticmethod
    def _score_replay_output(response_text: str) -> dict[str, Any]:
        """Score the replay branch from the replay result itself."""
        text = str(response_text or "").strip()
        if not text:
            return {
                "score": 0.0,
                "signal": "empty_response",
                "reason": "replay produced no assistant response",
            }
        lowered = text.lower()
        failure_markers = (
            "i can't",
            "i cannot",
            "无法完成",
            "不能完成",
            "抱歉",
            "sorry",
            "error",
            "failed",
        )
        if any(marker in lowered for marker in failure_markers):
            return {
                "score": 0.25,
                "signal": "uncertain_or_incomplete",
                "reason": "replay response contains failure or uncertainty markers",
            }
        return {
            "score": 0.75,
            "signal": "completed_response",
            "reason": "replay produced a concrete assistant response",
        }

    @staticmethod
    def _build_replay_skill_system(skill: Optional[dict[str, Any]]) -> str:
        if not isinstance(skill, dict) or not skill.get("name"):
            return ""
        return (
            "You are replaying a previously observed user task on this client machine.\n"
            "Apply the following local skill if it is relevant to the user instruction.\n"
            "If it does not apply, answer normally.\n\n"
            "<skill_file>\n"
            f"{build_skill_md(skill).strip()}\n"
            "</skill_file>"
        )

    @staticmethod
    def _build_replay_messages(case: dict[str, Any], skill: Optional[dict[str, Any]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        system_prompt = ValidationWorker._build_replay_skill_system(skill)
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        instruction = str(case.get("instruction", "") or "").strip()
        if instruction:
            messages.append({"role": "user", "content": instruction})
        return messages

    async def _run_replay_branch(
        self,
        case: dict[str, Any],
        skill: Optional[dict[str, Any]],
        *,
        label: str,
    ) -> dict[str, Any]:
        messages = self._build_replay_messages(case, skill)
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("replay case missing user instruction")
        response_text = await self._client.chat(
            messages,
            max_tokens=2048,
            temperature=0.1,
        )
        replay_result = self._score_replay_output(response_text)
        normalized_score = replay_result["score"]
        return {
            "label": label,
            "response_text": response_text,
            "replay_score": replay_result["score"],
            "replay_signal": replay_result["signal"],
            "replay_reason": replay_result["reason"],
            "normalized_score": normalized_score,
        }

    async def _replay_validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        candidate_skill = job.get("candidate_skill")
        if not isinstance(candidate_skill, dict) or not candidate_skill.get("name"):
            raise ValueError("validation job missing candidate_skill")

        replay_cases = [case for case in (job.get("replay_cases") or []) if isinstance(case, dict)]
        if not replay_cases:
            raise ValueError("validation job missing replay_cases")

        current_skill = job.get("current_skill") if isinstance(job.get("current_skill"), dict) else None
        case_results: list[dict[str, Any]] = []
        candidate_scores: list[float] = []
        baseline_scores: list[float] = []

        for case in replay_cases[:3]:
            baseline = await self._run_replay_branch(case, current_skill, label="baseline")
            candidate = await self._run_replay_branch(case, candidate_skill, label="candidate")
            baseline_score = baseline.get("normalized_score")
            candidate_score = candidate.get("normalized_score")
            if isinstance(baseline_score, (int, float)):
                baseline_scores.append(float(baseline_score))
            if isinstance(candidate_score, (int, float)):
                candidate_scores.append(float(candidate_score))
            case_results.append(
                {
                    "session_id": str(case.get("session_id", "") or ""),
                    "turn_num": int(case.get("turn_num", 0) or 0),
                    "instruction": str(case.get("instruction", "") or ""),
                    "baseline": baseline,
                    "candidate": candidate,
                }
            )

        if not candidate_scores:
            raise ValueError("replay validation produced no candidate scores")

        candidate_mean = round(sum(candidate_scores) / len(candidate_scores), 3)
        baseline_mean = round(sum(baseline_scores) / len(baseline_scores), 3) if baseline_scores else 0.0
        threshold = round(float(job.get("min_score", 0.75)), 3)
        improved = candidate_mean > baseline_mean
        accepted = candidate_mean >= threshold and improved
        decision = "accept" if accepted else "reject"
        reason = (
            f"Replay validation compared {len(case_results)} case(s): "
            f"candidate_mean={candidate_mean}, baseline_mean={baseline_mean}, "
            f"threshold={threshold}, improved={improved}"
        )
        return {
            "validator_mode": "replay",
            "decision": decision,
            "accepted": accepted,
            "score": candidate_mean,
            "threshold": threshold,
            "reason": reason,
            "checks": {
                "grounded_in_evidence": candidate_mean,
                "preserves_existing_value": min(1.0, max(0.0, candidate_mean - baseline_mean + 0.5)),
                "specificity_and_reusability": candidate_mean,
                "safe_to_publish": candidate_mean,
            },
            "replay_summary": {
                "case_count": len(case_results),
                "baseline_mean_score": baseline_mean,
                "candidate_mean_score": candidate_mean,
                "cases": case_results,
            },
        }

    async def _validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        if str(getattr(self.config, "validation_mode", "replay") or "replay").strip().lower() == "true_replay":
            job_id = str(job.get("job_id") or "")
            if job_id:
                try:
                    from ..true_replay import evaluate_job

                    replay = await asyncio.to_thread(evaluate_job, job_id, job=job)
                    if replay.get("status") == "evaluated":
                        return {
                            "validator_mode": "true_replay",
                            "decision": "accept" if replay.get("accepted") else "reject",
                            "accepted": bool(replay.get("accepted")),
                            "score": replay.get("score"),
                            "threshold": replay.get("threshold"),
                            "reason": (
                                f"True Replay score={replay.get('score')}, "
                                f"baseline={replay.get('baseline_mean')}, "
                                f"delta={replay.get('delta')}, quality_ok={replay.get('quality_ok')}"
                            ),
                            "checks": {
                                "grounded_in_evidence": replay.get("score"),
                                "preserves_existing_value": 1.0 if replay.get("no_regression") else 0.0,
                                "specificity_and_reusability": replay.get("score"),
                                "safe_to_publish": replay.get("score") if replay.get("accepted") else 0.0,
                            },
                            "replay_summary": replay,
                        }
                    logger.info("[ValidationWorker] true replay skipped for %s: %s", job_id, replay.get("reason"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ValidationWorker] true replay failed for %s: %s", job_id, exc)
        return await self._replay_validate_job(job)

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        summary = ValidationRunSummary()
        if not self._validation_enabled():
            summary.reason = "validation disabled or sharing not configured"
            return summary.__dict__
        if not self._quota_available():
            summary.reason = "daily validation quota reached"
            return summary.__dict__
        if not self._is_idle(force=force):
            summary.reason = "client is not idle"
            return summary.__dict__

        jobs = self._store.list_open_jobs(user_alias=self._user_alias)
        summary.checked_jobs = len(jobs)
        if not jobs:
            summary.reason = "no open validation jobs"
            return summary.__dict__

        for job in jobs:
            if not self._quota_available():
                summary.reason = "daily validation quota reached"
                break
            job_id = str(job.get("job_id", "") or "")
            if not job_id:
                summary.skipped_jobs += 1
                continue
            try:
                result = await self._validate_job(job)
            except Exception as exc:
                logger.warning("[ValidationWorker] job %s failed: %s", job_id, exc)
                summary.skipped_jobs += 1
                continue

            self._store.save_result(job_id, self._user_alias, result)
            self._jobs_completed_today += 1
            summary.validated_jobs += 1
            logger.info(
                "[ValidationWorker] submitted result for job %s as %s (score=%s)",
                job_id,
                self._user_alias,
                result.get("score"),
            )
            if summary.validated_jobs >= max(1, int(self.config.validation_max_concurrency)):
                break

        if summary.validated_jobs == 0 and not summary.reason:
            summary.reason = "no jobs validated"
        elif summary.validated_jobs > 0:
            summary.reason = "validated"
        return summary.__dict__

    async def run(self) -> None:
        interval = max(5, int(self.config.validation_poll_interval_seconds))
        logger.info(
            "[ValidationWorker] enabled=%s mode=%s interval=%ss idle_after=%ss",
            self.config.validation_enabled,
            self.config.validation_mode,
            interval,
            self.config.validation_idle_after_seconds,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("[ValidationWorker] polling loop failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def status_snapshot(self) -> dict[str, Any]:
        last_request_age = None
        active_sessions = None
        idle_now = None
        if self._idle_provider is not None:
            try:
                last_request_age = self._idle_provider.last_request_age_seconds()
                active_sessions = self._idle_provider.active_session_count()
                idle_now = self._idle_provider.is_idle_for_validation(
                    int(self.config.validation_idle_after_seconds),
                )
            except Exception:
                pass
        return {
            "enabled": bool(self.config.validation_enabled),
            "mode": str(self.config.validation_mode or "replay"),
            "sharing_enabled": bool(self.config.sharing_enabled),
            "customer_id": str(self.config.sharing_viking_customer_id or ""),
            "user_alias": self._user_alias,
            "idle_after_seconds": int(self.config.validation_idle_after_seconds),
            "poll_interval_seconds": int(self.config.validation_poll_interval_seconds),
            "max_jobs_per_day": int(self.config.validation_max_jobs_per_day),
            "jobs_completed_today": int(self._jobs_completed_today),
            "active_sessions": active_sessions,
            "last_request_age_seconds": last_request_age,
            "idle_now": idle_now,
            "open_jobs_for_me": len(self._store.list_open_jobs(user_alias=self._user_alias))
            if self._validation_enabled()
            else 0,
        }
