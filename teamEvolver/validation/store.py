"""Shared storage helpers for distributed client-side validation.

The validation flow uses the same object store boundary as the rest of
teamEvolver. Jobs are produced by the evolve server, validated by opted-in
clients, and later finalized by the evolve server.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..skills.bundle import candidate_skill_bundle, encode_bundle_payload
from ..storage import build_object_store, is_not_found_error, peer_key_prefix

logger = logging.getLogger(__name__)
_CLAIM_LOCK = threading.RLock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ValidationStore:
    """Persist validation jobs/results/decisions in shared storage."""

    def __init__(
        self,
        *,
        backend: str,
        endpoint: str,
        customer_id: str = "",
    ) -> None:
        self._bucket = build_object_store(
            backend=backend,
            endpoint=endpoint,
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
            raise ValueError("validation storage requires OpenViking object storage")
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

    def _candidate_skill_prefix(self, job_id: str) -> str:
        return f"{self._prefix()}candidate_skills/{job_id}/"

    def _candidate_bundle_file_key(self, job_id: str, rel_path: str) -> str:
        if rel_path == "SKILL.md":
            return self._candidate_skill_key(job_id)
        return f"{self._candidate_skill_prefix(job_id)}files/{rel_path}"

    def _result_key(self, job_id: str, user_alias: str) -> str:
        return f"{self._prefix()}validation_results/{job_id}/{user_alias}.json"

    def _decision_key(self, job_id: str) -> str:
        return f"{self._prefix()}validation_decisions/{job_id}.json"

    def _evaluation_key(self, job_id: str) -> str:
        return f"{self._prefix()}validation_evaluations/{job_id}.json"

    def _claim_key(self, job_id: str, user_alias: str) -> str:
        return (
            f"{self._prefix()}validation_claims/"
            f"{job_id}/{user_alias}.json"
        )

    def _decision_index_key(self) -> str:
        return f"{self._prefix()}validation_decision_index.json"

    def _open_job_index_key(self) -> str:
        return f"{self._prefix()}validation_open_jobs.json"

    def _skill_version_context_key(self, skill_name: str, version: int) -> str:
        return (
            f"{self._prefix()}skill_version_context/"
            f"{skill_name}/v{max(1, int(version))}.json"
        )

    def make_job_id(self, skill_name: str) -> str:
        slug = str(skill_name or "candidate").strip().lower().replace("_", "-")
        slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug).strip("-") or "candidate"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{timestamp}-{slug}-{uuid.uuid4().hex[:8]}"

    def _load_open_job_ids(self) -> list[str] | None:
        try:
            raw = json.loads(
                self._bucket.get_object(self._open_job_index_key())
                .read()
                .decode("utf-8")
            )
        except Exception as exc:
            return None if is_not_found_error(exc) else []
        return [str(item) for item in raw if str(item)] if isinstance(raw, list) else []

    def _save_open_job_ids(self, job_ids: list[str]) -> None:
        self._bucket.put_object(
            self._open_job_index_key(),
            json.dumps(list(dict.fromkeys(job_ids))[-500:], indent=2).encode("utf-8"),
        )

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
            bundle = candidate_skill_bundle(candidate_skill)
            keep_keys: set[str] = set()
            for rel_path, data in sorted(bundle.items()):
                key = self._candidate_bundle_file_key(job_id, rel_path)
                keep_keys.add(key)
                self._bucket.put_object(key, data)
            bundle_key = f"{self._candidate_skill_prefix(job_id)}bundle.json"
            keep_keys.add(bundle_key)
            self._bucket.put_object(
                bundle_key,
                json.dumps(
                    encode_bundle_payload(bundle),
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
            for obj in self._bucket.iter_objects(
                prefix=self._candidate_skill_prefix(job_id)
            ):
                if obj.key not in keep_keys:
                    self._bucket.delete_object(obj.key)
        open_ids = self._load_open_job_ids()
        if open_ids is not None and job_id not in open_ids:
            self._save_open_job_ids([*open_ids, job_id])

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

    def claim_job(
        self,
        job_id: str,
        user_alias: str,
        *,
        revision: int,
        lease_seconds: int = 1_500,
    ) -> str | None:
        """Atomically lease one job revision to one validator process."""
        key = self._claim_key(job_id, user_alias)
        now = datetime.now(timezone.utc)
        with _CLAIM_LOCK:
            existing: dict[str, Any] = {}
            precondition: dict[str, str] | None = None
            try:
                existing = json.loads(
                    self._bucket.get_object(key).read().decode("utf-8")
                )
            except Exception as exc:
                if not is_not_found_error(exc):
                    logger.warning(
                        "[ValidationStore] failed to read claim %s: %s",
                        key,
                        exc,
                    )
                    return None
            if bool(getattr(self._bucket, "native_batch_write", False)):
                precondition = self._bucket.object_precondition(key)
            try:
                expires_at = datetime.fromisoformat(
                    str(existing.get("expires_at") or "")
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expires_at = now
            if (
                existing
                and int(existing.get("candidate_revision") or 0)
                == int(revision)
                and expires_at > now
            ):
                return None
            token = uuid.uuid4().hex
            claim = {
                "job_id": job_id,
                "user_alias": user_alias,
                "candidate_revision": int(revision),
                "token": token,
                "claimed_at": now.isoformat(),
                "expires_at": (
                    now
                    + timedelta(seconds=max(30, int(lease_seconds)))
                ).isoformat(),
            }
            ensure_parent = getattr(self._bucket, "ensure_parent", None)
            if callable(ensure_parent):
                ensure_parent(key)
            data = json.dumps(
                claim,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            if bool(getattr(self._bucket, "native_batch_write", False)):
                try:
                    self._bucket.batch_write(
                        {key: data},
                        preconditions={key: dict(precondition or {})},
                    )
                except Exception:
                    return None
            else:
                self._bucket.put_object(key, data)
            try:
                persisted = json.loads(
                    self._bucket.get_object(key).read().decode("utf-8")
                )
            except Exception:
                return None
            return token if persisted.get("token") == token else None

    def release_job_claim(
        self,
        job_id: str,
        user_alias: str,
        token: str,
    ) -> bool:
        key = self._claim_key(job_id, user_alias)
        with _CLAIM_LOCK:
            try:
                claim = json.loads(
                    self._bucket.get_object(key).read().decode("utf-8")
                )
            except Exception:
                return False
            if str(claim.get("token") or "") != str(token or ""):
                return False
            try:
                self._bucket.delete_object(key)
            except Exception:
                return False
            return True

    def list_results(self, job_id: str) -> list[dict[str, Any]]:
        prefix = f"{self._prefix()}validation_results/{job_id}/"
        results: list[dict[str, Any]] = []
        try:
            objects = list(self._bucket.iter_objects(prefix=prefix))
        except Exception as exc:
            if is_not_found_error(exc):
                return []
            logger.warning(
                "[ValidationStore] failed to list results for %s: %s",
                job_id,
                exc,
            )
            return []
        for obj in objects:
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
        job = self.load_job(job_id) or {}
        evaluation = self.load_evaluation(job_id) or {}
        candidate = (
            job.get("candidate_skill")
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        compact_job = {
            key: job.get(key)
            for key in (
                "job_id",
                "candidate_skill_name",
                "candidate_skill_id",
                "proposed_action",
                "rationale",
                "evidence_classification",
                "session_ids",
                "min_score",
                "created_at",
            )
            if job.get(key) is not None
        }
        compact_job["candidate_skill"] = {
            key: candidate.get(key)
            for key in ("name", "description", "category", "skill_id", "edit_summary")
            if candidate.get(key) is not None
        }
        compact_job["replay_case_count"] = len(job.get("replay_cases") or [])
        compact_evaluation = deepcopy(evaluation)
        for key in (
            "candidate_skill",
            "current_skill",
            "candidate_skill_md",
            "current_skill_md",
            "skill_diff",
        ):
            compact_evaluation.pop(key, None)
        replay_summary = compact_evaluation.get("replay_summary")
        if isinstance(replay_summary, dict):
            replay_summary["cases"] = []
            replay_summary.pop("window_results", None)
        replay = compact_evaluation.get("replay")
        if isinstance(replay, dict):
            replay["cases"] = []
        record = {
            "job_id": job_id,
            "job": compact_job,
            "decision": payload,
            "evaluation": compact_evaluation,
            "decided_at": payload.get("decided_at"),
        }
        try:
            raw = json.loads(
                self._bucket.get_object(self._decision_index_key())
                .read()
                .decode("utf-8")
            )
            records = raw if isinstance(raw, list) else []
        except Exception:
            records = []
        records = [
            item
            for item in records
            if isinstance(item, dict) and item.get("job_id") != job_id
        ]
        records.append(record)
        records.sort(key=lambda item: str(item.get("decided_at") or ""), reverse=True)
        self._bucket.put_object(
            self._decision_index_key(),
            json.dumps(records[:200], ensure_ascii=False, indent=2).encode("utf-8"),
        )
        candidate = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else {}
        skill_name = str(
            payload.get("skill_name")
            or candidate.get("name")
            or job.get("candidate_skill_name")
            or ""
        ).strip()
        try:
            version = int(payload.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if payload.get("status") == "published" and skill_name and version > 0:
            version_record = {
                "job_id": job_id,
                "job": job,
                "decision": payload,
                "evaluation": evaluation,
                "decided_at": payload.get("decided_at"),
            }
            self._bucket.put_object(
                self._skill_version_context_key(skill_name, version),
                json.dumps(version_record, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        open_ids = self._load_open_job_ids()
        if open_ids is not None and job_id in open_ids:
            self._save_open_job_ids([value for value in open_ids if value != job_id])

    def load_decision(self, job_id: str) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self._bucket.get_object(self._decision_key(job_id)).read().decode("utf-8"))
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load decision %s: %s", job_id, exc)
            return None

    def list_decision_records(
        self,
        *,
        reconcile: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            raw = json.loads(
                self._bucket.get_object(self._decision_index_key())
                .read()
                .decode("utf-8")
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning("[ValidationStore] failed to load decision index: %s", exc)
            return []
        records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        return (
            self._reconcile_decision_records(records)
            if reconcile
            else records
        )

    def _reconcile_decision_records(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Refresh index rows whose per-job decision file has since advanced.

        The index caches a compacted decision per job. When the same job is
        re-published as a newer version, the authoritative per-job decision
        file advances but the index row can lag (stale-index drift). Re-read
        the decision file and adopt it whenever it is newer so the processed
        list reflects the latest published version.
        """
        reconciled: list[dict[str, Any]] = []
        changed = False
        for record in records:
            job_id = str(record.get("job_id") or "")
            current = self.load_decision(job_id) if job_id else None
            if isinstance(current, dict):
                indexed_decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
                if str(current.get("decided_at") or "") > str(indexed_decision.get("decided_at") or ""):
                    record = {**record, "decision": current, "decided_at": current.get("decided_at")}
                    changed = True
            reconciled.append(record)
        if changed:
            reconciled.sort(key=lambda item: str(item.get("decided_at") or ""), reverse=True)
        return reconciled

    def load_skill_version_context(
        self,
        skill_name: str,
        version: int,
    ) -> Optional[dict[str, Any]]:
        try:
            return json.loads(
                self._bucket.get_object(
                    self._skill_version_context_key(skill_name, version)
                )
                .read()
                .decode("utf-8")
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning(
                    "[ValidationStore] failed to load version context %s/v%s: %s",
                    skill_name,
                    version,
                    exc,
                )
            return None

    def save_evaluation(self, job_id: str, evaluation: dict[str, Any]) -> None:
        """Persist a non-binding True Replay metric evaluation.

        Unlike a decision, an evaluation does NOT close the job: it is the
        metric comparison a reviewer inspects before deciding whether to
        publish. Cached so the dashboard does not re-run the replay on every
        poll.
        """
        payload = dict(evaluation)
        payload["job_id"] = job_id
        payload.setdefault("evaluated_at", _utc_now_iso())
        # Stamp the revision this evaluation was computed against so a later
        # revision of the same job (re-generated / merged content) is detected
        # as stale rather than silently reused. See load_fresh_evaluation.
        if payload.get("candidate_revision") is None:
            job = self.load_job(job_id)
            payload["candidate_revision"] = max(1, int((job or {}).get("candidate_revision") or 1))
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

    def load_fresh_evaluation(
        self, job_id: str, job: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """Return the cached evaluation only if it matches the job's current
        candidate revision.

        A job whose candidate content was revised (e.g. re-generated or merged
        under the same job_id) bumps ``candidate_revision``. Reusing an
        evaluation computed against an older revision would stamp a stale A/B
        replay onto new content, so treat a revision mismatch as a cache miss
        and force a fresh evaluation.
        """
        cached = self.load_evaluation(job_id)
        if not isinstance(cached, dict):
            return None
        if job is None:
            job = self.load_job(job_id)
        job_revision = max(1, int((job or {}).get("candidate_revision") or 1))
        cached_revision = max(1, int(cached.get("candidate_revision") or 1))
        if cached_revision != job_revision:
            logger.info(
                "[ValidationStore] stale evaluation for %s: eval_revision=%d job_revision=%d",
                job_id,
                cached_revision,
                job_revision,
            )
            return None
        return cached

    @staticmethod
    def _replay_has_completed_metrics(source: dict[str, Any]) -> bool:
        """True when a result/evaluation carries a genuinely completed replay.

        A skipped or failed True Replay leaves every efficiency dimension at
        zero; a completed one has at least one non-zero baseline or candidate
        metric. Used to prefer a real prior comparison over a newer "skipped"
        auto-evaluation.
        """
        if not isinstance(source, dict):
            return False
        summary = (
            source.get("replay")
            if isinstance(source.get("replay"), dict)
            else source.get("replay_summary")
            if isinstance(source.get("replay_summary"), dict)
            else {}
        )
        efficiency = (
            summary.get("efficiency")
            if isinstance(summary.get("efficiency"), dict)
            else {}
        )
        dimensions = efficiency.get("dimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            return False
        return any(
            isinstance(metric, dict)
            and (
                int(metric.get("baseline") or 0) != 0
                or int(metric.get("candidate") or 0) != 0
            )
            for metric in dimensions.values()
        )

    def load_best_evaluation(
        self, job_id: str, job: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """Best evaluation for the job's current revision.

        Prefers the most recent source (cached evaluation or per-user result)
        that actually completed a True Replay, so a transient replay failure
        recorded as a newer "skipped" evaluation never blanks out a real prior
        metric comparison. Falls back to the newest source otherwise.
        """
        if job is None:
            job = self.load_job(job_id)
        revision = max(1, int((job or {}).get("candidate_revision") or 1))

        def _time(source: dict[str, Any]) -> str:
            return str(
                source.get("created_at") or source.get("evaluated_at") or ""
            )

        sources: list[dict[str, Any]] = []
        cached = self.load_evaluation(job_id)
        if isinstance(cached, dict) and cached:
            if max(1, int(cached.get("candidate_revision") or 1)) == revision:
                sources.append(cached)
        for result in self.list_results(job_id):
            if not isinstance(result, dict) or not result:
                continue
            if max(1, int(result.get("candidate_revision") or 1)) == revision:
                sources.append(result)
        if not sources:
            return None
        completed = [
            source
            for source in sources
            if self._replay_has_completed_metrics(source)
        ]
        pool = completed or sources
        return max(pool, key=_time)

    def list_open_jobs(self, *, user_alias: str = "") -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        indexed_ids = self._load_open_job_ids()
        source_jobs = (
            [
                job
                for job_id in indexed_ids
                if (job := self.load_job(job_id)) is not None
            ]
            if indexed_ids is not None
            else self.list_jobs()
        )
        for job in source_jobs:
            job_id = str(job.get("job_id", "") or "")
            if not job_id:
                continue
            if self.load_decision(job_id):
                continue
            if user_alias:
                result = self.load_result(job_id, user_alias)
                if result:
                    revision = max(1, int(job.get("candidate_revision") or 1))
                    result_revision = int(result.get("candidate_revision") or 1)
                    if result_revision == revision and (
                        result.get("candidate_revision") is not None or revision == 1
                    ):
                        continue
            jobs.append(job)
        return jobs

    def list_open_jobs_for_skill(self, skill_name: str) -> list[dict[str, Any]]:
        wanted = str(skill_name or "").strip()
        if not wanted:
            return []
        matches: list[dict[str, Any]] = []
        for job in self.list_open_jobs():
            candidate = (
                job.get("candidate_skill")
                if isinstance(job.get("candidate_skill"), dict)
                else {}
            )
            name = str(
                candidate.get("name") or job.get("candidate_skill_name") or ""
            ).strip()
            if name == wanted:
                matches.append(job)
        return matches

    def find_open_job_for_skill(self, skill_name: str) -> Optional[dict[str, Any]]:
        matches = self.list_open_jobs_for_skill(skill_name)
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
        )

    def reset_job_artifacts(self, job_id: str) -> dict[str, Any]:
        """Clear revision-bound outputs while retaining the validation job."""
        removed: list[str] = []
        keys = [
            self._evaluation_key(job_id),
            self._human_review_key(job_id),
        ]
        result_prefix = f"{self._prefix()}validation_results/{job_id}/"
        try:
            keys.extend(obj.key for obj in self._bucket.iter_objects(prefix=result_prefix))
            keys.extend(
                obj.key
                for obj in self._bucket.iter_objects(
                    prefix=self._candidate_skill_prefix(job_id)
                )
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning(
                    "[ValidationStore] failed to list stale results for %s: %s",
                    job_id,
                    exc,
                )
        for key in keys:
            try:
                self._bucket.delete_object(key)
                removed.append(key)
            except Exception as exc:
                if not is_not_found_error(exc):
                    logger.warning(
                        "[ValidationStore] failed to clear stale artifact %s: %s",
                        key,
                        exc,
                    )
        return {"job_id": job_id, "removed": removed}

    # -- human-in-the-loop review queue ------------------------------- #

    def _human_review_key(self, job_id: str) -> str:
        return f"{self._prefix()}human_review/{job_id}.json"

    def save_human_review_task(
        self,
        job_id: str,
        task: dict[str, Any],
    ) -> None:
        payload = dict(task)
        payload["job_id"] = job_id
        payload.setdefault("created_at", _utc_now_iso())
        payload["updated_at"] = _utc_now_iso()
        self._bucket.put_object(
            self._human_review_key(job_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def load_human_review_task(
        self,
        job_id: str,
    ) -> Optional[dict[str, Any]]:
        try:
            return json.loads(
                self._bucket.get_object(self._human_review_key(job_id))
                .read()
                .decode("utf-8")
            )
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.warning(
                    "[ValidationStore] failed to load human review %s: %s",
                    job_id,
                    exc,
                )
            return None

    def list_human_review_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        prefix = f"{self._prefix()}human_review/"
        for obj in self._bucket.iter_objects(prefix=prefix):
            if not obj.key.endswith(".json"):
                continue
            try:
                value = json.loads(
                    self._bucket.get_object(obj.key).read().decode("utf-8")
                )
                if isinstance(value, dict):
                    tasks.append(value)
            except Exception as exc:
                logger.warning(
                    "[ValidationStore] failed to parse %s: %s",
                    obj.key,
                    exc,
                )
        tasks.sort(
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
            reverse=True,
        )
        return tasks

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """Remove a validation job and all its side artifacts.

        Deletes the job record, the rendered candidate SKILL.md, any cached
        evaluation, the human-review task, and any per-user results. Best-effort
        per key so a missing artifact never blocks the rest. Returns which keys
        were actually removed so the caller can report/verify."""
        removed: list[str] = []
        keys = [
            self._job_key(job_id),
            self._evaluation_key(job_id),
            self._decision_key(job_id),
            self._human_review_key(job_id),
        ]
        # Per-user result objects live under a job-scoped prefix.
        try:
            result_prefix = f"{self._prefix()}validation_results/{job_id}/"
            for obj in self._bucket.iter_objects(prefix=result_prefix):
                keys.append(obj.key)
            for obj in self._bucket.iter_objects(
                prefix=self._candidate_skill_prefix(job_id)
            ):
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
        open_ids = self._load_open_job_ids()
        if open_ids is not None and job_id in open_ids:
            self._save_open_job_ids([item for item in open_ids if item != job_id])
        return {"job_id": job_id, "removed": removed}
