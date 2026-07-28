"""Shared storage helpers for distributed client-side validation.

The validation flow uses the same object store boundary as the rest of
teamEvolver. Jobs are produced by the evolve server, validated by opted-in
clients, and later finalized by the evolve server.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..storage import build_object_store, is_not_found_error, peer_key_prefix
from ..skills.render import build_skill_md

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ValidationStore:
    """Persist validation jobs/results/decisions in shared storage."""

    def __init__(
        self,
        *,
        backend: str,
        endpoint: str,
        local_root: str = "",
        customer_id: str = "",
    ) -> None:
        self._bucket = build_object_store(
            backend=backend,
            endpoint=endpoint,
            local_root=local_root,
        )
        self._customer_id = str(customer_id or "").strip("/")

    @classmethod
    def from_bucket(cls, *, bucket, customer_id: str = "") -> "ValidationStore":
        """Reuse an already-constructed object store."""
        store = cls.__new__(cls)
        store._bucket = bucket
        store._customer_id = str(customer_id or "").strip("/")
        return store

    @classmethod
    def from_config(cls, config) -> "ValidationStore":
        from ..skills.hub import SkillHub

        hub = SkillHub.object_storage_from_config(config)
        if hub is None:
            raise ValueError("validation storage requires local or viking object storage")
        store = cls.__new__(cls)
        store._bucket = hub._bucket
        store._customer_id = str(getattr(config, "sharing_viking_customer_id", "") or "").strip("/")
        return store

    def _prefix(self) -> str:
        return peer_key_prefix(self._customer_id)

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix()}validation_jobs/{job_id}.json"

    def _candidate_skill_key(self, job_id: str) -> str:
        return f"{self._prefix()}candidate_skills/{job_id}/SKILL.md"

    def _result_key(self, job_id: str, user_alias: str) -> str:
        return f"{self._prefix()}validation_results/{job_id}/{user_alias}.json"

    def _decision_key(self, job_id: str) -> str:
        return f"{self._prefix()}validation_decisions/{job_id}.json"

    def _evaluation_key(self, job_id: str) -> str:
        return f"{self._prefix()}validation_evaluations/{job_id}.json"

    def make_job_id(self, skill_name: str) -> str:
        slug = str(skill_name or "candidate").strip().lower().replace("_", "-")
        slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug).strip("-") or "candidate"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{timestamp}-{slug}-{uuid.uuid4().hex[:8]}"

    def save_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id", "") or "")
        if not job_id:
            raise ValueError("validation job requires job_id")
        payload = dict(job)
        payload.setdefault("created_at", _utc_now_iso())
        self._bucket.put_object(
            self._job_key(job_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        candidate_skill = payload.get("candidate_skill")
        if isinstance(candidate_skill, dict) and candidate_skill.get("name"):
            self._bucket.put_object(
                self._candidate_skill_key(job_id),
                build_skill_md(candidate_skill).encode("utf-8"),
            )

    def load_job(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._job_key(job_id)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load job %s: %s", job_id, exc)
            return None

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        prefix = f"{self._prefix()}validation_jobs/"
        for obj in self._bucket.iter_objects(prefix=prefix):
            if not obj.key.endswith(".json"):
                continue
            try:
                jobs.append(json.loads(self._bucket.get_object(obj.key).read().decode("utf-8")))
            except Exception as exc:
                logger.warning("[ValidationStore] failed to parse %s: %s", obj.key, exc)
        jobs.sort(key=lambda item: str(item.get("created_at", "")))
        return jobs

    def save_result(self, job_id: str, user_alias: str, result: dict[str, Any]) -> None:
        payload = dict(result)
        payload["job_id"] = job_id
        payload["user_alias"] = user_alias
        payload.setdefault("created_at", _utc_now_iso())
        self._bucket.put_object(
            self._result_key(job_id, user_alias),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_result(self, job_id: str, user_alias: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._result_key(job_id, user_alias)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning(
                    "[ValidationStore] failed to load result for %s/%s: %s",
                    job_id,
                    user_alias,
                    exc,
                )
            return None

    def list_results(self, job_id: str) -> list[dict[str, Any]]:
        prefix = f"{self._prefix()}validation_results/{job_id}/"
        results: list[dict[str, Any]] = []
        for obj in self._bucket.iter_objects(prefix=prefix):
            if not obj.key.endswith(".json"):
                continue
            try:
                results.append(json.loads(self._bucket.get_object(obj.key).read().decode("utf-8")))
            except Exception as exc:
                logger.warning("[ValidationStore] failed to parse %s: %s", obj.key, exc)
        results.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("user_alias", ""))))
        return results

    def save_decision(self, job_id: str, decision: dict[str, Any]) -> None:
        payload = dict(decision)
        payload["job_id"] = job_id
        payload.setdefault("decided_at", _utc_now_iso())
        self._bucket.put_object(
            self._decision_key(job_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_decision(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._decision_key(job_id)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load decision %s: %s", job_id, exc)
            return None

    def save_evaluation(self, job_id: str, evaluation: dict[str, Any]) -> None:
        """Persist a non-binding dry-run evaluation (verify + A/B replay scores).

        Unlike a decision, an evaluation does NOT close the job: it is the
        score preview a reviewer inspects before deciding whether to publish.
        Cached so the dashboard can show scores without re-running the LLM
        replay on every poll.
        """
        payload = dict(evaluation)
        payload["job_id"] = job_id
        payload.setdefault("evaluated_at", _utc_now_iso())
        self._bucket.put_object(
            self._evaluation_key(job_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_evaluation(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._evaluation_key(job_id)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load evaluation %s: %s", job_id, exc)
            return None

    def list_open_jobs(self, *, user_alias: str = "") -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for job in self.list_jobs():
            job_id = str(job.get("job_id", "") or "")
            if not job_id:
                continue
            if self.load_decision(job_id):
                continue
            if user_alias and self.load_result(job_id, user_alias):
                continue
            jobs.append(job)
        return jobs

    # -- human-in-the-loop review queue ------------------------------- #

    def _human_review_key(self, job_id: str) -> str:
        return f"{self._prefix()}human_review/{job_id}.json"

    def save_human_review_task(self, job_id: str, task: dict[str, Any]) -> None:
        payload = dict(task)
        payload["job_id"] = job_id
        payload.setdefault("created_at", _utc_now_iso())
        self._bucket.put_object(
            self._human_review_key(job_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_human_review_task(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._human_review_key(job_id)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load human review task %s: %s", job_id, exc)
            return None

    def list_human_review_tasks(self) -> list[dict[str, Any]]:
        prefix = f"{self._prefix()}human_review/"
        tasks: list[dict[str, Any]] = []
        for obj in self._bucket.iter_objects(prefix=prefix):
            if not obj.key.endswith(".json"):
                continue
            try:
                tasks.append(json.loads(self._bucket.get_object(obj.key).read().decode("utf-8")))
            except Exception as exc:
                logger.warning("[ValidationStore] failed to parse %s: %s", obj.key, exc)
        tasks.sort(key=lambda item: str(item.get("created_at", "")))
        return tasks

    def delete_human_review_task(self, job_id: str) -> None:
        try:
            self._bucket.delete_object(self._human_review_key(job_id))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to delete human review task %s: %s", job_id, exc)

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Remove a validation job and all its side artifacts.

        Deletes the job record, the rendered candidate SKILL.md, any cached
        evaluation, the human-review task, and any per-user results. Best-effort
        per key so a missing artifact never blocks the rest. Returns which keys
        were actually removed so the caller can report/verify."""
        removed: list[str] = []
        keys = [
            self._job_key(job_id),
            self._candidate_skill_key(job_id),
            self._evaluation_key(job_id),
            self._decision_key(job_id),
            self._human_review_key(job_id),
        ]
        # Per-user result objects live under a job-scoped prefix.
        try:
            result_prefix = f"{self._prefix()}validation_results/{job_id}/"
            for obj in self._bucket.iter_objects(prefix=result_prefix):
                keys.append(obj.key)
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to list results for %s: %s", job_id, exc)
        for key in keys:
            try:
                self._bucket.delete_object(key)
                removed.append(key)
            except Exception as exc:
                if not is_not_found_error(exc):
                    logger.warning("[ValidationStore] failed to delete %s: %s", key, exc)
        return {"job_id": job_id, "removed": removed}
