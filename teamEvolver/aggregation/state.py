"""Incremental state + per-group status for memory aggregation.

Deterministic staging and semantic merge have deliberately separate invalidation
rules:

- a user snapshot is restaged only when its source inventory changed, its prior
  staging failed, or a full run was requested;
- a merge group is recompiled when an input snapshot/intermediate changed, its
  prior compile failed, the Skill changed, or a full run was requested.

State is a small JSON file under the configured state dir; it is intentionally
tolerant of partial/corrupt files so a bad state never blocks a run.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AggregationState:
    """Load/save aggregation fingerprints and group status."""

    path: Path
    account_id: str
    skill_fingerprint: str = ""
    # group_key -> {"status": "ok"|"failed", "source_fingerprint": str}
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def journal_path(self) -> Path:
        return self.path.with_name(self.path.name + ".journal")

    @classmethod
    def load(cls, path: str | os.PathLike[str], account_id: str) -> "AggregationState":
        p = Path(path).expanduser()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        groups = raw.get("groups")
        state = cls(
            path=p,
            account_id=account_id,
            skill_fingerprint=str(raw.get("skill_fingerprint", "") or ""),
            groups=groups if isinstance(groups, dict) else {},
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )
        try:
            journal_lines = state.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            journal_lines = []
        for line in journal_lines:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(record, dict)
                or record.get("account") != account_id
                or not isinstance(record.get("entry"), dict)
            ):
                continue
            group_key = str(record.get("group_key") or "")
            if not group_key:
                continue
            state.skill_fingerprint = str(
                record.get("skill_fingerprint") or state.skill_fingerprint
            )
            state.groups[group_key] = record["entry"]
        return state

    def needs_recompile(
        self,
        group_key: str,
        source_fingerprint: str,
        *,
        current_skill_fingerprint: str,
        full: bool = False,
    ) -> bool:
        if full:
            return True
        if current_skill_fingerprint != self.skill_fingerprint:
            return True
        entry = self.groups.get(group_key)
        if not isinstance(entry, dict):
            return True
        if str(entry.get("status")) == "failed":
            return True
        return str(entry.get("source_fingerprint", "")) != source_fingerprint

    def needs_restage(
        self,
        group_key: str,
        source_fingerprint: str,
        *,
        staging_uri: str,
        full: bool = False,
    ) -> bool:
        if full:
            return True
        entry = self.groups.get(group_key)
        if not isinstance(entry, dict):
            return True
        if str(entry.get("status")) == "failed":
            return True
        return (
            str(entry.get("source_fingerprint", "")) != source_fingerprint
            or str(entry.get("staging_uri", "")) != staging_uri
        )

    def mark_ok(self, group_key: str, source_fingerprint: str) -> None:
        self.groups[group_key] = {
            "status": "ok",
            "source_fingerprint": source_fingerprint,
        }

    def mark_stage_ok(
        self,
        group_key: str,
        source_fingerprint: str,
        *,
        staging_uri: str,
        source_count: int,
        total_bytes: int,
    ) -> None:
        self.groups[group_key] = {
            "status": "ok",
            "source_fingerprint": source_fingerprint,
            "staging_uri": staging_uri,
            "source_count": source_count,
            "total_bytes": total_bytes,
        }

    def mark_failed(self, group_key: str) -> None:
        entry = self.groups.get(group_key)
        failed = dict(entry) if isinstance(entry, dict) else {}
        # Preserve the prior snapshot metadata for diagnostics, but failed
        # status always forces the next staging/merge attempt to retry.
        failed["status"] = "failed"
        failed.setdefault("source_fingerprint", "")
        self.groups[group_key] = failed

    def checkpoint(self, group_key: str, *, skill_fingerprint: str) -> None:
        """Append one durable group update without rewriting the full state."""
        entry = self.groups.get(group_key)
        if not isinstance(entry, dict):
            raise ValueError(f"cannot checkpoint unknown aggregation group: {group_key}")
        self.skill_fingerprint = skill_fingerprint
        record = {
            "account": self.account_id,
            "skill_fingerprint": skill_fingerprint,
            "group_key": group_key,
            "entry": entry,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(
            self.journal_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    def save(self, *, skill_fingerprint: str) -> None:
        self.skill_fingerprint = skill_fingerprint
        payload = {
            "account": self.account_id,
            "skill_fingerprint": skill_fingerprint,
            "groups": self.groups,
            "metadata": self.metadata,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                self.journal_path.unlink()
            except FileNotFoundError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
