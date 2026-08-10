"""Optional background validator for idle clients.

This worker is intentionally conservative:
- it is disabled by default
- it only runs when sharing is enabled
- it only picks up jobs when the local client appears idle
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from ..checklist import (
    aggregate_branch_checklist_results,
    compile_common_checklist,
    objective_replay_decision,
)
from ..llm import AsyncLLMClient
from ..skills.bundle import bundle_tree_sha256, candidate_skill_bundle
from ..skills.render import build_skill_md
from .bundle_checks import validate_candidate_bundle
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

    CAPABILITIES = frozenset(
        {
            "bundle_v1",
            "bundle_static_v1",
            "bundle_true_replay_v1",
        }
    )

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

    async def _score_replay_output_with_rubric(
        self,
        case: dict[str, Any],
        response_text: str,
    ) -> dict[str, Any]:
        """Judge a mined benchmark answer against its private gold rubric."""
        gold = case.get("gold") if isinstance(case.get("gold"), dict) else {}
        if not gold:
            return self._score_replay_output(response_text)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict benchmark judge. Score the assistant answer against "
                    "the private rubric. Reward expected labels and must_hit requirements; "
                    "penalize any must_avoid violation or invented fact. Reply only as JSON "
                    'with {"score": number from 0 to 1, "reason": "short reason"}.'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Instruction:\n{case.get('instruction', '')}\n\n"
                    f"Private rubric:\n{json.dumps(gold, ensure_ascii=False)}\n\n"
                    f"Assistant answer:\n{response_text}"
                ),
            },
        ]
        try:
            raw = await self._client.chat(
                messages,
                max_tokens=512,
                temperature=0.0,
            )
            start, end = raw.find("{"), raw.rfind("}")
            parsed = (
                json.loads(raw[start : end + 1])
                if start >= 0 and end >= start
                else {}
            )
            score = max(0.0, min(1.0, float(parsed.get("score"))))
            return {
                "score": round(score, 3),
                "signal": "gold_rubric",
                "reason": str(
                    parsed.get("reason") or "internal benchmark rubric score"
                )[:800],
            }
        except Exception:  # noqa: BLE001
            return self._score_replay_output(response_text)

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
        replay_result = await self._score_replay_output_with_rubric(
            case,
            response_text,
        )
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
        window_scores: dict[str, dict[str, list[float]]] = {}

        for case in replay_cases[:3]:
            window = str(case.get("evidence_window") or "recent")
            if window not in {"recent", "historical"}:
                window = "recent"
            scores = window_scores.setdefault(
                window,
                {"baseline": [], "candidate": []},
            )
            baseline = await self._run_replay_branch(case, current_skill, label="baseline")
            candidate = await self._run_replay_branch(case, candidate_skill, label="candidate")
            baseline_score = baseline.get("normalized_score")
            candidate_score = candidate.get("normalized_score")
            if isinstance(baseline_score, (int, float)):
                baseline_scores.append(float(baseline_score))
                scores["baseline"].append(float(baseline_score))
            if isinstance(candidate_score, (int, float)):
                candidate_scores.append(float(candidate_score))
                scores["candidate"].append(float(candidate_score))
            case_results.append(
                {
                    "evidence_window": window,
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
        recent = window_scores.get("recent") or next(iter(window_scores.values()))
        recent_candidate = (
            sum(recent["candidate"]) / len(recent["candidate"])
            if recent["candidate"]
            else 0.0
        )
        recent_baseline = (
            sum(recent["baseline"]) / len(recent["baseline"])
            if recent["baseline"]
            else 0.0
        )
        recent_improved = (
            recent_candidate >= threshold and recent_candidate > recent_baseline
        )
        historical = window_scores.get("historical")
        historical_no_regression = True
        if historical and historical["candidate"]:
            historical_candidate = sum(historical["candidate"]) / len(
                historical["candidate"]
            )
            historical_baseline = (
                sum(historical["baseline"]) / len(historical["baseline"])
                if historical["baseline"]
                else 0.0
            )
            historical_no_regression = (
                historical_candidate >= historical_baseline - 0.15
            )
        accepted = recent_improved and historical_no_regression
        if accepted:
            decision = "accept"
        elif not historical_no_regression:
            decision = "reject"
        else:
            decision = "inconclusive"
        reason = (
            f"Replay validation compared {len(case_results)} case(s): "
            f"candidate_mean={candidate_mean}, baseline_mean={baseline_mean}, "
            f"threshold={threshold}, recent_improved={recent_improved}, "
            f"historical_no_regression={historical_no_regression}"
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
                "recent_improved": recent_improved,
                "historical_no_regression": historical_no_regression,
                "cases": case_results,
            },
        }

    @staticmethod
    def _aggregate_true_replay_windows(
        results: list[tuple[str, dict[str, Any]]],
        *,
        checklist: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evaluated = [
            (window, result)
            for window, result in results
            if str(result.get("status") or "") in {"", "evaluated"}
        ]
        if not evaluated:
            return {
                "status": "skipped",
                "accepted": False,
                "no_regression": False,
                "reason": "no replay window produced an evaluation",
                "window_results": {window: result for window, result in results},
            }
        by_window = {window: result for window, result in evaluated}
        recent = by_window.get("recent") or evaluated[0][1]
        historical = by_window.get("historical")
        all_windows_evaluated = len(evaluated) == len(results)
        recent_improved = bool(recent.get("accepted"))
        historical_no_regression = (
            True if historical is None else bool(historical.get("no_regression"))
        )

        cases: list[dict[str, Any]] = []
        for window, result in evaluated:
            for raw_case in result.get("cases") or []:
                if isinstance(raw_case, dict):
                    case = dict(raw_case)
                    case["evidence_window"] = window
                    cases.append(case)
        full_checklist = checklist or recent.get("checklist") or {}
        window_checklist_results = {
            window: (
                result.get("checklist_results")
                if isinstance(result.get("checklist_results"), dict)
                else {}
            )
            for window, result in evaluated
        }
        branch_results = {
            branch: aggregate_branch_checklist_results(
                full_checklist,
                [
                    result.get(branch) or {}
                    for result in window_checklist_results.values()
                    if isinstance(result.get(branch), dict)
                ],
            )
            for branch in ("baseline", "candidate")
        }
        efficiency_inputs = {"baseline": {}, "candidate": {}}
        for _, result in evaluated:
            report = (
                result.get("efficiency")
                if isinstance(result.get("efficiency"), dict)
                else {}
            )
            for branch in ("baseline", "candidate"):
                values = (
                    report.get(branch)
                    if isinstance(report.get(branch), dict)
                    else {}
                )
                for key in (
                    "interaction_turns",
                    "tool_call_count",
                    "total_tokens",
                ):
                    efficiency_inputs[branch][key] = int(
                        efficiency_inputs[branch].get(key) or 0
                    ) + int(values.get(key) or 0)
        from ..true_replay import compare_efficiency

        efficiency = compare_efficiency(
            efficiency_inputs["baseline"],
            efficiency_inputs["candidate"],
        )
        policy = objective_replay_decision(
            checklist=full_checklist,
            baseline=branch_results["baseline"],
            candidate=branch_results["candidate"],
            efficiency=efficiency,
        )
        accepted = bool(policy.get("accepted")) and all_windows_evaluated
        verdict = str(policy.get("verdict") or "inconclusive")
        if not all_windows_evaluated and verdict == "accept":
            verdict = "inconclusive"
        no_regression = (
            bool(policy.get("no_regression")) and all_windows_evaluated
        )
        return {
            "status": "evaluated",
            "mode": "true_replay",
            "accepted": accepted,
            "verdict": verdict,
            "no_regression": no_regression,
            "recent_improved": recent_improved,
            "historical_no_regression": historical_no_regression,
            "quality_ok": bool(policy.get("quality_gate"))
            and historical_no_regression,
            "score": branch_results["candidate"]["pass_rate"],
            "baseline_mean": branch_results["baseline"]["pass_rate"],
            "delta": round(
                branch_results["candidate"]["pass_rate"]
                - branch_results["baseline"]["pass_rate"],
                4,
            ),
            "threshold": recent.get("threshold"),
            "tolerance": recent.get("tolerance"),
            "case_count": sum(
                int(result.get("case_count") or 0)
                for _, result in evaluated
            ),
            "cases": cases,
            "efficiency": efficiency,
            "checklist": full_checklist,
            "checklist_results": {
                **branch_results,
                "windows": window_checklist_results,
            },
            "decision_policy": {
                **policy,
                "accepted": accepted,
                "historical_no_regression": historical_no_regression,
                "all_windows_evaluated": all_windows_evaluated,
            },
            "window_results": {window: result for window, result in results},
        }

    async def _validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        candidate = (
            job.get("candidate_skill")
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        static_validation = validate_candidate_bundle(
            candidate,
            enabled=bool(
                getattr(
                    self.config,
                    "evolve_bundle_static_checks_enabled",
                    True,
                )
            ),
        )
        if not static_validation.get("passed"):
            return {
                "validator_mode": "bundle_static",
                "decision": "reject",
                "accepted": False,
                "score": 0.0,
                "threshold": float(job.get("min_score", 0.75) or 0.75),
                "reason": "Candidate bundle failed deterministic static checks.",
                "checks": {"safe_to_publish": 0.0},
                "static_validation": static_validation,
                "replay_summary": {
                    "status": "skipped",
                    "reason": "static validation failed",
                    "cases": [],
                },
            }
        if str(getattr(self.config, "validation_mode", "true_replay") or "true_replay").strip().lower() == "true_replay":
            job_id = str(job.get("job_id") or "")
            if job_id:
                try:
                    from ..true_replay import evaluate_job

                    selected: list[tuple[str, int]] = []
                    seen_windows: set[str] = set()
                    for index, case in enumerate(job.get("replay_cases") or []):
                        if not isinstance(case, dict):
                            continue
                        window = str(case.get("evidence_window") or "recent")
                        if window not in {"recent", "historical"}:
                            window = "recent"
                        if window in seen_windows:
                            continue
                        selected.append((window, index))
                        seen_windows.add(window)
                    window_results: list[tuple[str, dict[str, Any]]] = []
                    for window, case_index in selected:
                        result = await asyncio.to_thread(
                            evaluate_job,
                            job_id,
                            job=job,
                            case_index=case_index,
                        )
                        window_results.append((window, result))
                    replay = self._aggregate_true_replay_windows(
                        window_results,
                        checklist=compile_common_checklist(job),
                    )
                    if replay.get("status") == "evaluated":
                        return {
                            "validator_mode": "true_replay",
                            "decision": str(
                                replay.get("verdict")
                                or (
                                    "accept"
                                    if replay.get("accepted")
                                    else "inconclusive"
                                )
                            ),
                            "accepted": bool(replay.get("accepted")),
                            "score": replay.get("score"),
                            "threshold": replay.get("threshold"),
                            "reason": (
                                f"Checklist score={replay.get('score')}, "
                                f"baseline={replay.get('baseline_mean')}, "
                                f"efficiency={((replay.get('decision_policy') or {}).get('efficiency_score'))}, "
                                f"accepted={replay.get('accepted')}"
                            ),
                            "checks": {
                                "grounded_in_evidence": replay.get("score"),
                                "preserves_existing_value": 1.0 if replay.get("no_regression") else 0.0,
                                "specificity_and_reusability": replay.get("score"),
                                "safe_to_publish": replay.get("score") if replay.get("accepted") else 0.0,
                            },
                            "static_validation": static_validation,
                            "replay_summary": replay,
                        }
                    logger.info("[ValidationWorker] true replay skipped for %s: %s", job_id, replay.get("reason"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ValidationWorker] true replay failed for %s: %s", job_id, exc)
        result = await self._replay_validate_job(job)
        result["static_validation"] = static_validation
        return result

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
            required_capabilities = {
                str(item)
                for item in (job.get("required_validator_capabilities") or [])
                if str(item)
            }
            if not required_capabilities.issubset(self.CAPABILITIES):
                logger.info(
                    "[ValidationWorker] skipped %s due to unsupported capabilities: %s",
                    job_id,
                    sorted(required_capabilities - self.CAPABILITIES),
                )
                summary.skipped_jobs += 1
                continue
            try:
                result = await self._validate_job(job)
            except Exception as exc:
                logger.warning("[ValidationWorker] job %s failed: %s", job_id, exc)
                summary.skipped_jobs += 1
                continue

            candidate_revision = max(1, int(job.get("candidate_revision") or 1))
            latest = self._store.load_job(job_id)
            latest_revision = (
                max(1, int(latest.get("candidate_revision") or 1))
                if isinstance(latest, dict)
                else 0
            )
            if latest_revision != candidate_revision:
                logger.info(
                    "[ValidationWorker] discarded stale result for %s revision=%d latest=%d",
                    job_id,
                    candidate_revision,
                    latest_revision,
                )
                summary.skipped_jobs += 1
                continue
            result["candidate_revision"] = candidate_revision
            result["validator_capabilities"] = sorted(self.CAPABILITIES)
            candidate = (
                job.get("candidate_skill")
                if isinstance(job.get("candidate_skill"), dict)
                else {}
            )
            result["candidate_bundle_tree_sha256"] = bundle_tree_sha256(
                candidate_skill_bundle(candidate)
            )
            self._store.save_result(job_id, self._user_alias, result)
            self._jobs_completed_today += 1
            summary.validated_jobs += 1
            logger.info(
                "[ValidationWorker] submitted result for job %s as %s (score=%s)",
                job_id,
                self._user_alias,
                result.get("score"),
            )
            finalized = await self._trigger_evolve_finalize()
            if finalized and result.get("accepted") is True:
                expected = await self._wait_for_published_commit(job)
                if expected is not None:
                    await self._sync_agentshub_skills(job, expected)
            if summary.validated_jobs >= max(1, int(self.config.validation_max_concurrency)):
                break

        if summary.validated_jobs == 0 and not summary.reason:
            summary.reason = "no jobs validated"
        elif summary.validated_jobs > 0:
            summary.reason = "validated"
        return summary.__dict__

    async def _trigger_evolve_finalize(self) -> bool:
        """Wake the evolve cycle after a validation vote is persisted."""
        base_url = str(getattr(self.config, "evolve_server_url", "") or "").rstrip("/")
        if not base_url:
            base_url = f"http://127.0.0.1:{int(getattr(self.config, 'proxy_port', 52010) or 52010)}"
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{base_url}/trigger")
                response.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001 - periodic cycle remains the fallback.
            logger.warning("[ValidationWorker] failed to trigger evolve finalize: %s", exc)
            return False

    async def _wait_for_published_commit(
        self,
        job: dict[str, Any],
        *,
        timeout_seconds: float = 180.0,
    ) -> dict[str, Any] | None:
        """Wait until finalize commits a versioned bundle selected by the manifest."""
        job_id = str(job.get("job_id") or "")
        candidate = (
            job.get("candidate_skill")
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        name = str(candidate.get("name") or job.get("candidate_skill_name") or "")
        if not job_id or not name:
            return None
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            decision = self._store.load_decision(job_id) or {}
            status = str(decision.get("status") or "")
            if status in {"rejected", "skipped"}:
                return None
            if status == "published":
                try:
                    from ..skills.hub import SkillHub

                    hub = SkillHub.team_from_config(self.config)
                    record = next(
                        (
                            item
                            for item in hub.list_remote()
                            if str(item.get("name") or "") == name
                        ),
                        None,
                    )
                    version = int((record or {}).get("version") or 0)
                    expected_sha = str((record or {}).get("sha256") or "")
                    expected_tree = str((record or {}).get("tree_sha256") or "")
                    bundle = (
                        hub._read_version_bundle(name, version)
                        if version > 0
                        else {}
                    )
                    markdown = bundle.get("SKILL.md", b"")
                    actual_sha = hashlib.sha256(markdown).hexdigest() if markdown else ""
                    actual_tree = bundle_tree_sha256(bundle) if bundle else ""
                    if (
                        version > 0
                        and expected_sha
                        and actual_sha == expected_sha
                        and (not expected_tree or actual_tree == expected_tree)
                    ):
                        return {
                            "name": name,
                            "version": version,
                            "sha256": expected_sha,
                            "tree_sha256": expected_tree or actual_tree,
                        }
                except Exception:  # noqa: BLE001 - finalize may still be committing.
                    pass
            await asyncio.sleep(1.0)

        self._save_agentshub_sync_status(
            job_id,
            status="failed",
            detail="timed out waiting for committed published bundle",
        )
        logger.warning(
            "[ValidationWorker] timed out waiting for published commit for %s",
            job_id,
        )
        return None

    def _save_agentshub_sync_status(
        self,
        job_id: str,
        *,
        status: str,
        expected: dict[str, Any] | None = None,
        detail: Any = None,
    ) -> None:
        decision = self._store.load_decision(job_id)
        if not decision:
            return
        decision["agentshub_sync"] = {
            "status": status,
            "expected": dict(expected or {}),
            "detail": detail,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._store.save_decision(job_id, decision)

    async def _sync_agentshub_skills(
        self,
        job: dict[str, Any],
        expected: dict[str, Any],
    ) -> dict[str, Any] | None:
        base_url = str(getattr(self.config, "validation_agentshub_url", "") or "").rstrip("/")
        if not base_url:
            return None
        job_id = str(job.get("job_id") or "")
        tenant_ids = self._source_tenant_ids(job)
        if not tenant_ids:
            self._save_agentshub_sync_status(
                job_id,
                status="failed",
                expected=expected,
                detail="source sessions do not contain an AgentsHub tenant",
            )
            return None
        headers: dict[str, str] = {}
        api_key = str(
            getattr(self.config, "validation_agentshub_api_key", "") or ""
        ).strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._save_agentshub_sync_status(
            job_id,
            status="syncing",
            expected=expected,
        )
        import httpx

        last_error = ""
        for attempt in range(8):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        f"{base_url}/api/internal/team-evolver/sync",
                        json={
                            "tenant_ids": tenant_ids,
                            "expected_skills": [expected],
                        },
                        headers=headers,
                    )
                    response.raise_for_status()
                    payload = response.json()
                self._save_agentshub_sync_status(
                    job_id,
                    status="synced",
                    expected=expected,
                    detail=payload,
                )
                return payload
            except Exception as exc:  # noqa: BLE001 - retry committed visibility.
                last_error = str(exc)
                if attempt < 7:
                    await asyncio.sleep(2.0)
        self._save_agentshub_sync_status(
            job_id,
            status="failed",
            expected=expected,
            detail=last_error,
        )
        logger.warning(
            "[ValidationWorker] AgentsHub verified skill sync callback failed: %s",
            last_error,
        )
        return None

    def _source_tenant_ids(self, job: dict[str, Any]) -> list[str]:
        try:
            from ..session_store import SessionStore

            store = SessionStore.from_config(self.config)
        except Exception:
            return []
        tenant_ids: list[str] = []
        for session_id in job.get("session_ids") or []:
            source = store.load_session(str(session_id or ""))
            context = (
                source.get("runtime_context")
                if isinstance(source, dict)
                and isinstance(source.get("runtime_context"), dict)
                else {}
            )
            tenant_id = str(context.get("tenant_id") or "").strip()
            if tenant_id and tenant_id not in tenant_ids:
                tenant_ids.append(tenant_id)
        return tenant_ids

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
