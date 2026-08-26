"""Incremental state + per-group status for memory aggregation.

Mirrors the three-condition re-refine model from the original algorithm design
(``docs`` in the customer scenario), but keyed by compile batch group rather
than by a bespoke refiner. A group is recompiled when any of:

1. a source file under it changed (content fingerprint differs), or
2. its last run failed (``status == "failed"``), or
3. the skill fingerprint changed (rule change -> full recompile).

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
        return cls(
            path=p,
            account_id=account_id,
            skill_fingerprint=str(raw.get("skill_fingerprint", "") or ""),
            groups=groups if isinstance(groups, dict) else {},
        )

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

    def mark_ok(self, group_key: str, source_fingerprint: str) -> None:
        self.groups[group_key] = {
            "status": "ok",
            "source_fingerprint": source_fingerprint,
        }

    def mark_failed(self, group_key: str) -> None:
        entry = self.groups.get(group_key)
        prior = entry.get("source_fingerprint", "") if isinstance(entry, dict) else ""
        # Roll back the fingerprint so the next run re-triggers via condition 1
        # even if the skill fingerprint is unchanged.
        self.groups[group_key] = {"status": "failed", "source_fingerprint": prior}

    def save(self, *, skill_fingerprint: str) -> None:
        self.skill_fingerprint = skill_fingerprint
        payload = {
            "account": self.account_id,
            "skill_fingerprint": skill_fingerprint,
            "groups": self.groups,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
