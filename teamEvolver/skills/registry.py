"""
Persistent skill-name -> skill-id registry with version tracking.

The registry is stored next to synced skills as ``evolve_skill_registry.json``.
The filename is kept for compatibility with existing teamEvolver deployments.
"""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SkillIDRegistry:
    """Maintains a persistent mapping: skill_name -> {skill_id, version, ...}."""

    def __init__(self) -> None:
        self._map: dict[str, dict[str, Any]] = {}

    def load_from_oss(self, bucket, prefix: str) -> None:
        key = f"{prefix}evolve_skill_registry.json"
        try:
            data = bucket.get_object(key).read().decode("utf-8")
            raw = json.loads(data)
            if isinstance(raw, dict):
                self._map = self._normalise(raw)
            logger.info("[SkillIDRegistry] loaded %d entries from storage", len(self._map))
        except Exception:
            logger.info("[SkillIDRegistry] no existing registry in storage; starting fresh")

    def save_to_oss(self, bucket, prefix: str) -> None:
        key = f"{prefix}evolve_skill_registry.json"
        content = json.dumps(self._map, ensure_ascii=False, indent=2)
        bucket.put_object(key, content.encode("utf-8"))

    @staticmethod
    def _normalise(raw: dict) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, val in raw.items():
            if isinstance(val, str):
                out[name] = {"skill_id": val, "version": 1, "content_sha": "", "history": []}
            elif isinstance(val, dict):
                out[name] = val
            else:
                out[name] = {
                    "skill_id": hashlib.sha256(str(name).encode()).hexdigest()[:12],
                    "version": 1,
                    "content_sha": "",
                    "history": [],
                }
        return out

    def get_or_create(self, skill_name: str) -> str:
        entry = self._map.get(skill_name)
        if entry:
            return str(entry["skill_id"])
        sid = hashlib.sha256(skill_name.encode()).hexdigest()[:12]
        self._map[skill_name] = {
            "skill_id": sid,
            "version": 0,
            "content_sha": "",
            "history": [],
        }
        return sid

    def record_update(
        self,
        skill_name: str,
        content_sha: str,
        action: str = "create",
        *,
        bundle_record: Optional[dict[str, Any]] = None,
    ) -> int:
        entry = self._map.get(skill_name)
        if not entry:
            self.get_or_create(skill_name)
            entry = self._map[skill_name]

        new_version = int(entry.get("version") or 0) + 1
        entry["version"] = new_version
        entry["content_sha"] = content_sha
        if isinstance(bundle_record, dict):
            for key in ("format", "entrypoint", "tree_sha256"):
                if bundle_record.get(key):
                    entry[key] = bundle_record[key]
            files = bundle_record.get("files")
            if isinstance(files, list):
                entry["files"] = deepcopy(files)

        history: list = entry.setdefault("history", [])
        item: dict[str, Any] = {
            "version": new_version,
            "content_sha": content_sha,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
        }
        if isinstance(bundle_record, dict):
            for key in ("format", "entrypoint", "tree_sha256"):
                if bundle_record.get(key):
                    item[key] = bundle_record[key]
            files = bundle_record.get("files")
            if isinstance(files, list):
                item["files"] = deepcopy(files)
        history.append(item)
        if len(history) > 20:
            entry["history"] = history[-20:]
        return new_version

    def all_ids(self) -> dict[str, str]:
        return {name: str(entry["skill_id"]) for name, entry in self._map.items()}
