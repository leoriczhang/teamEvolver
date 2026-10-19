"""Session request, acceptance response and immutable Worker payload contracts.

The accepted payload fixes tenant/user identity, content hashes and configuration
and adapter revisions. Required stages must succeed before formal archive."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Fixed ingestion sources (mirrors the ``source`` CHECK in 002_session_ingestion.sql).
SOURCE_PUSH = "push"
SOURCE_PULL = "pull"
INGESTION_SOURCES = (SOURCE_PUSH, SOURCE_PULL)

# The mandatory analysis stages. A formal Session is only written after every
# stage in this tuple has a ``succeeded`` row in ``session_stage_results``
#.
MANDATORY_STAGES = ("normalize", "split", "analyze", "trace_analyze", "archive")

# The schema version stamped onto every enqueued task payload, so a worker can
# reject a payload shape it does not understand instead of guessing.
PAYLOAD_SCHEMA_VERSION = "session-ingestion.v1"


@dataclass(frozen=True)
class AcceptedJob:
    """What the API returns after a successful 202 acceptance."""

    ingestion_id: str
    job_id: str
    status: str
    dedupe: bool = False

    @property
    def status_url(self) -> str:
        return f"/api/jobs/{self.job_id}"

    def to_response(self) -> dict[str, Any]:
        return {
            "accepted": True,
            "ingestion_id": self.ingestion_id,
            "job_id": self.job_id,
            "status": self.status,
            "status_url": self.status_url,
            "dedupe": self.dedupe,
        }


@dataclass
class IngestionTaskPayload:
    """Immutable payload pinned into a ``session.*`` worker task.

    The payload fixes the ``config_revision`` and ``adapter_revision`` a run uses
    so a later config/adapter change cannot alter an in-flight task. The normalized session
    envelope lives in the raw payload artifact referenced by ``payload_ref``; this
    payload only carries the small, hashable control fields.
    """

    schema_version: str
    source: str
    ingestion_id: str
    tenant_id: str
    external_id: str
    user_id: str
    account_id: str
    config_revision: int = 0
    adapter_id: str = ""
    adapter_revision: int = 0
    payload_ref: str = ""
    payload_hash: str = ""
    # Pull-only control: the fetch filters/window and the adapter binding file.
    pull: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise to canonical, sorted-key JSON (stable hash input)."""
        return json.dumps(self._as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def payload_fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def _as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "ingestion_id": self.ingestion_id,
            "tenant_id": self.tenant_id,
            "external_id": self.external_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "config_revision": int(self.config_revision),
            "adapter_id": self.adapter_id,
            "adapter_revision": int(self.adapter_revision),
            "payload_ref": self.payload_ref,
            "payload_hash": self.payload_hash,
            "pull": self.pull or {},
        }

    @classmethod
    def from_json(cls, raw: str | bytes) -> "IngestionTaskPayload":
        data = json.loads(raw)
        return cls(
            schema_version=str(data.get("schema_version") or ""),
            source=str(data.get("source") or ""),
            ingestion_id=str(data.get("ingestion_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            external_id=str(data.get("external_id") or ""),
            user_id=str(data.get("user_id") or ""),
            account_id=str(data.get("account_id") or ""),
            config_revision=int(data.get("config_revision") or 0),
            adapter_id=str(data.get("adapter_id") or ""),
            adapter_revision=int(data.get("adapter_revision") or 0),
            payload_ref=str(data.get("payload_ref") or ""),
            payload_hash=str(data.get("payload_hash") or ""),
            pull=dict(data.get("pull") or {}),
        )


def _json_default(value: Any) -> Any:
    """Coerce the non-JSON-native types the analysis stages inject into a session.

    The real analyze/summarize stages populate ``set`` metadata
    (``_skills_referenced`` / ``_skills_injected``); ``json.dumps`` rejects a
    ``set``. Sort it to a stable list so the content hash is deterministic across
    runs. Any other unexpected type still raises, so a genuinely unserializable
    payload is not silently accepted.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def session_payload_hash(session: dict[str, Any]) -> str:
    """Stable hash of a raw session envelope (used as the artifact content hash)."""
    raw = json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "SOURCE_PUSH",
    "SOURCE_PULL",
    "INGESTION_SOURCES",
    "MANDATORY_STAGES",
    "PAYLOAD_SCHEMA_VERSION",
    "AcceptedJob",
    "IngestionTaskPayload",
    "session_payload_hash",
]
