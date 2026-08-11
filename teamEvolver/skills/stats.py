"""Per-skill injection/feedback statistics.

Tracks how often each skill is injected into a request and the feedback it
subsequently receives, deriving an effectiveness score. Persisted as
``skill_stats.json`` beside the skills directory and flushed every N mutations
to avoid excessive I/O.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_EFFECTIVENESS = 0.5


def _new_entry() -> dict[str, Any]:
    return {
        "inject_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "neutral_count": 0,
        "last_injected_at": "",
        "effectiveness": _DEFAULT_EFFECTIVENESS,
    }


class SkillStats:
    """Injection/feedback counters backed by a JSON file."""

    def __init__(self, path: str, flush_every: int = 10):
        self._path = path
        self._flush_every = max(1, int(flush_every))
        self._stats: dict[str, dict[str, Any]] = self._load()
        self._dirty = 0

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict[str, dict[str, Any]]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[SkillStats] failed to load stats: %s", e)
            return {}

    def save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("[SkillStats] failed to save stats: %s", e)
        self._dirty = 0

    def _maybe_flush(self) -> None:
        """Persist every ``flush_every`` mutations to bound I/O."""
        self._dirty += 1
        if self._dirty >= self._flush_every:
            self.save()

    def reload(self) -> None:
        """Flush pending changes and reload counters from disk."""
        self.save()
        self._stats = self._load()
        self._dirty = 0

    # ------------------------------------------------------------------ #
    # Mutations                                                            #
    # ------------------------------------------------------------------ #

    def record_injection(self, skill_names: list[str]) -> None:
        """Record that *skill_names* were injected into a request."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for name in skill_names:
            entry = self._stats.setdefault(name, _new_entry())
            entry["inject_count"] += 1
            entry["last_injected_at"] = now
        self._maybe_flush()

    def record_feedback(self, skill_names: list[str], score: float) -> None:
        """Record feedback for skills injected in a turn."""
        for name in skill_names:
            entry = self._stats.get(name)
            if entry is None:
                continue
            if score > 0:
                entry["positive_count"] += 1
            elif score < 0:
                entry["negative_count"] += 1
            else:
                entry["neutral_count"] += 1
            total = entry["inject_count"]
            entry["effectiveness"] = entry["positive_count"] / total if total > 0 else _DEFAULT_EFFECTIVENESS
        self._maybe_flush()

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def effectiveness(self, skill_name: str) -> float:
        """Effectiveness score for *skill_name* (default ``0.5`` if unknown)."""
        entry = self._stats.get(skill_name)
        if entry is None:
            return _DEFAULT_EFFECTIVENESS
        return entry.get("effectiveness", _DEFAULT_EFFECTIVENESS)
