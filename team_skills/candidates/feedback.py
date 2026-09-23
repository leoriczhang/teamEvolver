"""Durable, tenant-scoped human feedback, independent of replay artifacts."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teamEvolver.storage import LocalObjectStore
from teamEvolver.storage.admin_kv import _pg_store
from teamEvolver.storage.pg_store import validate_tenant_id

_FEEDBACK_LOCK = threading.RLock()
_FIELDS = ("reviewed", "adopted", "rejected")


class FeedbackConflictError(RuntimeError):
    pass


class CandidateFeedbackStore:
    """Keep checkbox state and its audit history in one atomic record.

    Uses the admin-state PG backend when enabled, otherwise LocalObjectStore.
    Feedback survives candidate edits, replay resets and publication.
    """

    def __init__(self, bucket) -> None:
        self._bucket = bucket

    @classmethod
    def from_config(cls, config, *, tenant_id: str = "default") -> "CandidateFeedbackStore":
        tenant_id = validate_tenant_id(tenant_id)
        bucket = _pg_store(config, tenant_id)
        if bucket is None:
            root = str(
                getattr(config, "sharing_skill_local_root", "")
                or getattr(config, "sharing_local_root", "")
                or ""
            )
            if not root:
                registry = str(getattr(config, "users_registry_path", "") or "")
                root = str(Path(registry).expanduser().parent if registry else Path.home() / ".teamEvolver")
            bucket = LocalObjectStore(Path(root) / "tenants" / tenant_id)
        return cls(bucket)

    @staticmethod
    def _key(job_id: str) -> str:
        # Candidate IDs are opaque; never let them become filesystem paths.
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return f"_admin/candidate_feedback/{digest}.json"

    def _read(self, job_id: str) -> tuple[dict[str, Any], bytes | None]:
        try:
            raw = self._bucket.get_object(self._key(job_id)).read()
        except FileNotFoundError:
            return {
                "reviewed": False,
                "adopted": False,
                "rejected": False,
                "version": 0,
                "history": [],
            }, None
        data = json.loads(raw.decode("utf-8"))
        # Records written before the rejected marker existed remain valid.
        if isinstance(data, dict):
            data.setdefault("rejected", False)
        if (
            not isinstance(data, dict)
            or data.get("job_id") != job_id
            or any(type(data.get(field)) is not bool for field in _FIELDS)
            or not isinstance(data.get("history"), list)
        ):
            raise ValueError("candidate feedback record is invalid")
        return data, raw

    def load(self, job_id: str) -> dict[str, Any]:
        return self._read(job_id)[0]

    def update(
        self,
        job_id: str,
        changes: dict[str, bool],
        *,
        actor_id: str,
        actor_name: str,
        candidate_revision: int,
    ) -> dict[str, Any]:
        if not changes or any(key not in _FIELDS or type(value) is not bool for key, value in changes.items()):
            raise ValueError("candidate feedback fields must be boolean values")
        if not actor_id:
            raise ValueError("feedback requires an authenticated actor")
        with _FEEDBACK_LOCK:
            for _attempt in range(3):
                data, raw = self._read(job_id)
                changed = {key: value for key, value in changes.items() if data[key] != value}
                if not changed:
                    return data
                now = datetime.now(timezone.utc).isoformat()
                version = int(data.get("version") or 0) + 1
                for field, value in changed.items():
                    data["history"].append({
                        "field": field,
                        "value": value,
                        "actor_id": actor_id,
                        "actor_name": actor_name or actor_id,
                        "at": now,
                        "candidate_revision": candidate_revision,
                        "version": version,
                    })
                    data[field] = value
                data.update(job_id=job_id, updated_at=now, version=version)
                key = self._key(job_id)
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                if not getattr(self._bucket, "native_batch_write", False):
                    self._bucket.put_object(key, payload)
                    return data
                condition = (
                    {"kind": "replace_if_hash", "base_hash": "sha256:" + hashlib.sha256(raw).hexdigest()}
                    if raw is not None else {"kind": "create_if_absent"}
                )
                try:
                    self._bucket.batch_write({key: payload}, preconditions={key: condition})
                    return data
                except RuntimeError as exc:
                    if "precondition conflict" not in str(exc):
                        raise
            raise FeedbackConflictError("候选标记正在被其他用户更新，请重试")
