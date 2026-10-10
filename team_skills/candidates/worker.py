"""Centralized background validator.

This worker is intentionally conservative:
- it evaluates a candidate only while the service appears idle
- it aggregates all selected Replay cases into one authoritative result
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from team_replay.aggregation import aggregate_true_replay_windows
from team_replay.contracts import skill_replay_spec
from team_replay.gateway import EmbeddedReplayGateway
from team_replay.policy import (
    select_replay_cases,
)
from team_replay.runtime_matrix import (
    evaluate_runtime_compatibility,
    runtime_type_for_case,
)
from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.candidates.store import CENTRAL_VALIDATOR_ID, ValidationStore
from team_skills.library.bundle import bundle_tree_sha256, candidate_skill_bundle

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
    """Idle-time validator owned by the centralized teamEvolver service."""

    CAPABILITIES = frozenset(
        {
            "skill-bundle",
            "bundle-static",
            "bundle-true-replay",
        }
    )

    def __init__(
        self,
        config,
        *,
        idle_provider: Optional[IdleStateProvider] = None,
        llm_client: Any = None,
        tenant_id: str = "default",
        tenant_context: Any = None,
    ) -> None:
        del llm_client
        from teamEvolver.storage.pg_store import validate_tenant_id

        self.config = config
        self.tenant_id = validate_tenant_id(tenant_id)
        if (
            tenant_context is not None
            and str(getattr(tenant_context, "tenant_id", "") or "")
            != self.tenant_id
        ):
            raise ValueError("tenant_context does not match tenant_id")
        self._tenant_context = tenant_context
        self._idle_provider = idle_provider
        self._store = ValidationStore.from_config(
            config,
            tenant_id=self.tenant_id,
        )
        self._stop_event = asyncio.Event()
        self._jobs_completed_today = 0
        self._jobs_completed_date = datetime.now(timezone.utc).date().isoformat()
        # A stable ID makes active replicas claim and overwrite the same
        # authoritative result.
        self._validator_id = CENTRAL_VALIDATOR_ID
        from team_replay.engine import evaluate_job
        from teamEvolver.replay_adapter import ensure_replay_host

        ensure_replay_host()
        self._replay_gateway = EmbeddedReplayGateway(evaluate_job)
        self._load_quota()

    def stop(self) -> None:
        self._stop_event.set()

    def _validation_enabled(self) -> bool:
        return bool(self.config.validation_enabled)

    def _reset_daily_quota_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._jobs_completed_date:
            self._jobs_completed_date = today
            self._jobs_completed_today = 0
        self._save_quota()

    def _quota_file(self) -> Path:
        """Per-tenant quota file so multi-tenant deployments don't share a counter."""
        if self.tenant_id == "default":
            return Path.home() / ".teamEvolver" / "validation_quota.json"
        return (
            Path.home()
            / ".teamEvolver"
            / f"validation_quota.{self.tenant_id}.json"
        )

    def _save_quota(self) -> None:
        path = self._quota_file()
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(
                    {
                        "date": self._jobs_completed_date,
                        "count": self._jobs_completed_today,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("[ValidationWorker] failed to persist validation quota", exc_info=True)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _load_quota(self) -> None:
        path = self._quota_file()
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        stored_date = str(data.get("date") or "")
        if stored_date == datetime.now(timezone.utc).date().isoformat():
            self._jobs_completed_date = stored_date
            self._jobs_completed_today = int(data.get("count") or 0)

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
    def _aggregate_true_replay_windows(
        results: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        return aggregate_true_replay_windows(results)

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
                "reason": "Candidate bundle failed deterministic static checks.",
                "static_validation": static_validation,
                "replay_summary": {
                    "status": "skipped",
                    "reason": "static validation failed",
                    "cases": [],
                },
            }
        replay: dict[str, Any] = {}
        validation_mode = str(
            getattr(self.config, "validation_mode", "true_replay")
            or "true_replay"
        ).strip().lower()
        if validation_mode == "true_replay":
            job_id = str(job.get("job_id") or "")
            if job_id:
                try:
                    selected = select_replay_cases(
                        job.get("replay_cases") or []
                    )
                    replay_spec = skill_replay_spec(
                        job,
                        account_id=str(
                            getattr(self.config, "viking_account", "")
                            or getattr(self.config, "sharing_viking_account", "")
                            or "default"
                        ),
                    )
                    window_results: list[tuple[str, dict[str, Any]]] = []
                    results_by_runtime: dict[
                        str,
                        list[tuple[str, dict[str, Any]]],
                    ] = {}
                    replay_cases = list(job.get("replay_cases") or [])
                    for window, case_index in selected:
                        result = await asyncio.to_thread(
                            self._replay_gateway.evaluate,
                            replay_spec,
                            case_index=case_index,
                            max_interactions=max(
                                1,
                                int(job.get("max_interactions") or 4),
                            ),
                        )
                        window_results.append((window, result))
                        runtime_type = (
                            runtime_type_for_case(replay_cases[case_index])
                            or "unknown"
                        )
                        results_by_runtime.setdefault(
                            runtime_type,
                            [],
                        ).append((window, result))
                    replay = self._aggregate_true_replay_windows(
                        window_results,
                    )
                    runtime_validation = {
                        runtime_type: self._aggregate_true_replay_windows(
                            runtime_results
                        )
                        for runtime_type, runtime_results
                        in results_by_runtime.items()
                    }
                    runtime_gate = evaluate_runtime_compatibility(
                        (
                            job.get("runtime_validation_policy")
                            if isinstance(
                                job.get("runtime_validation_policy"),
                                dict,
                            )
                            else {}
                        ),
                        [{"runtime_validation": runtime_validation}],
                    )
                    replay["runtime_validation"] = runtime_validation
                    replay["runtime_gate"] = runtime_gate
                    if runtime_gate["status"] != "passed":
                        replay["accepted"] = False
                        replay["verdict"] = (
                            "reject"
                            if runtime_gate["status"] == "rejected"
                            else "inconclusive"
                        )
                        replay["reason"] = runtime_gate["reason"]
                    if replay.get("status") == "evaluated":
                        policy = (
                            replay.get("decision_policy")
                            if isinstance(replay.get("decision_policy"), dict)
                            else {}
                        )
                        changes = policy.get("metric_changes") or {}
                        reason = ", ".join(
                            f"{name}: {item.get('baseline', 0)} -> "
                            f"{item.get('candidate', 0)}"
                            for name, item in changes.items()
                            if isinstance(item, dict)
                        )
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
                            "reason": reason,
                            "static_validation": static_validation,
                            "replay_summary": replay,
                            "runtime_validation": runtime_validation,
                            "runtime_gate": runtime_gate,
                        }
                    logger.info("[ValidationWorker] true replay skipped for %s: %s", job_id, replay.get("reason"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ValidationWorker] true replay failed for %s: %s", job_id, exc)
                    replay = {
                        "status": "skipped",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "cases": [],
                    }
        return {
            "validator_mode": "true_replay",
            "decision": "inconclusive",
            "accepted": False,
            "reason": str(
                (replay if isinstance(replay, dict) else {}).get("reason")
                or "true replay produced no complete result"
            ),
            "static_validation": static_validation,
            "replay_summary": (
                replay
                if isinstance(replay, dict)
                else {
                    "status": "skipped",
                    "reason": "true replay unavailable",
                    "cases": [],
                }
            ),
        }

    def _bind_tenant_context(self):
        from teamEvolver.tenants.registry import (
            TenantContext,
            get_current_tenant,
            set_current_tenant,
        )

        context = self._tenant_context
        if context is None:
            current = get_current_tenant()
            if (
                current is not None
                and current.tenant_id == self.tenant_id
            ):
                context = current
            else:
                context = TenantContext(tenant_id=self.tenant_id)
        return set_current_tenant(context)

    async def run_once(self, *, force: bool = False) -> dict[str, Any]:
        from teamEvolver.tenants.registry import reset_current_tenant

        token = self._bind_tenant_context()
        try:
            return await self._run_once(force=force)
        finally:
            reset_current_tenant(token)

    async def _run_once(self, *, force: bool = False) -> dict[str, Any]:
        summary = ValidationRunSummary()
        if not self._validation_enabled():
            summary.reason = "validation disabled"
            return summary.__dict__
        if not self._quota_available():
            summary.reason = "daily validation quota reached"
            return summary.__dict__
        if not self._is_idle(force=force):
            summary.reason = "service is not idle"
            return summary.__dict__

        jobs = self._store.list_open_jobs(validator_id=self._validator_id)
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
            candidate_revision = max(
                1,
                int(job.get("candidate_revision") or 1),
            )
            claim_token = self._store.claim_job(
                job_id,
                self._validator_id,
                revision=candidate_revision,
                lease_seconds=(
                    max(
                        300,
                        int(
                            getattr(
                                self.config,
                                "validation_replay_timeout_seconds",
                                600,
                            )
                            or 600
                        )
                        * 2
                        + 300,
                    )
                ),
            )
            if not claim_token:
                logger.info(
                    "[ValidationWorker] skipped %s because another process "
                    "holds the validation lease",
                    job_id,
                )
                summary.skipped_jobs += 1
                continue
            try:
                result = await self._validate_job(job)
            except Exception as exc:
                logger.warning("[ValidationWorker] job %s failed: %s", job_id, exc)
                self._store.release_job_claim(
                    job_id,
                    self._validator_id,
                    claim_token,
                )
                summary.skipped_jobs += 1
                continue

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
                self._store.release_job_claim(
                    job_id,
                    self._validator_id,
                    claim_token,
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
            self._store.save_result(job_id, self._validator_id, result)
            # Candidate/replay UIs read the evaluation object, while the
            # centralized publish gate reads the authoritative worker result.
            # Persist both projections so automatic validation is immediately visible.
            self._store.save_evaluation(job_id, result)
            self._store.release_job_claim(
                job_id,
                self._validator_id,
                claim_token,
            )
            self._jobs_completed_today += 1
            self._save_quota()
            summary.validated_jobs += 1
            logger.info(
                "[ValidationWorker] submitted result for job %s as %s "
                "(decision=%s)",
                job_id,
                self._validator_id,
                result.get("decision"),
            )
            finalized = await self._trigger_evolve_finalize()
            if finalized and result.get("accepted") is True:
                expected = await self._wait_for_published_commit(job)
                if expected is not None:
                    await self._record_skill_delivery(job, expected)
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

            key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "").strip()
            headers = {"X-Tenant-Id": self.tenant_id}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{base_url}/trigger", headers=headers)
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
                    from team_skills.library.hub import SkillHub

                    hub = SkillHub.team_from_config(
                        self.config,
                        tenant_id=self.tenant_id,
                    )
                    record = next(
                        (
                            item
                            for item in hub.list_skills()
                            if str(item.get("name") or "") == name
                        ),
                        None,
                    )
                    version = int((record or {}).get("version") or 0)
                    expected_sha = str((record or {}).get("sha256") or "")
                    expected_tree = str((record or {}).get("tree_sha256") or "")
                    bundle = (
                        hub.read_version_bundle(name, version)
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

        logger.warning(
            "[ValidationWorker] timed out waiting for published commit for %s",
            job_id,
        )
        return None

    async def _record_skill_delivery(
        self,
        job: dict[str, Any],
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        from team_skills.library.mutations import SkillMutationService

        job_id = str(job.get("job_id") or "")
        service = SkillMutationService.from_config(
            self.config,
            tenant_id=self.tenant_id,
        )
        await asyncio.to_thread(
            service.record_committed,
            action="publish",
            mutation_id=job_id,
            expected=expected,
            tenant_ids=self._source_tenant_ids(job),
        )
        payload = {"status": "published", "delivery_mode": "pull"}
        decision = self._store.load_decision(job_id)
        if decision:
            decision["skill_delivery"] = {
                **payload,
                "expected": dict(expected),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            decision.pop("agent_sync", None)
            self._store.save_decision(job_id, decision)
        return payload

    def _source_tenant_ids(self, job: dict[str, Any]) -> list[str]:
        try:
            from teamEvolver.session_store import SessionStore

            store = SessionStore.from_config(
                self.config,
                tenant_id=self.tenant_id,
            )
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
            "tenant_id": self.tenant_id,
            "enabled": bool(self.config.validation_enabled),
            "mode": str(self.config.validation_mode or "replay"),
            "validator_id": self._validator_id,
            "idle_after_seconds": int(self.config.validation_idle_after_seconds),
            "poll_interval_seconds": int(self.config.validation_poll_interval_seconds),
            "max_jobs_per_day": int(self.config.validation_max_jobs_per_day),
            "jobs_completed_today": int(self._jobs_completed_today),
            "active_sessions": active_sessions,
            "last_request_age_seconds": last_request_age,
            "idle_now": idle_now,
            "open_jobs_for_me": len(self._store.list_open_jobs(validator_id=self._validator_id))
            if self._validation_enabled()
            else 0,
        }
