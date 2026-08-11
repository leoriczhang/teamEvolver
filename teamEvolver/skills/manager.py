"""Skill library loading, injection, and effectiveness tracking.

Loads skills from a directory of AgentSkills-compatible skill folders::

    <skills_dir>/
        skill-name/
            SKILL.md           <- YAML frontmatter + markdown body
            scripts/ | references/ | assets/   <- optional bundle files

Each ``SKILL.md`` has YAML frontmatter with at least ``name`` and
``description`` (see :mod:`teamEvolver.skills.frontmatter`). All eligible skills
are injected into the model's system prompt as an ``<available_skills>``
catalog; there is no per-request retrieval — selection is delegated to the
model via the injected instructions.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any

from . import frontmatter, layout, prompt
from .bundle import list_skill_bundle_paths
from .stats import SkillStats

logger = logging.getLogger(__name__)

_STATS_FILENAME = "skill_stats.json"


class SkillManager:
    """Loads AgentSkills-compatible skills and builds the injection catalog.

    All loaded skills (minus those marked ``disable-model-invocation``) are
    injected; the model chooses which to read based on the catalog.
    """

    def __init__(self, skills_dir: str, public_skill_root: str = ""):
        if not os.path.isdir(skills_dir):
            raise FileNotFoundError(f"Skills directory not found: {skills_dir}")

        self._skills_dir = skills_dir
        self._public_skill_root = public_skill_root.strip()

        # Monotonically-increasing counter, bumped whenever the local skill
        # library changes so callers can drop stale snapshots.
        self.generation: int = 0

        self.skills = self._load_skills()
        self._skills_fingerprint = self._compute_skills_fingerprint()
        self._stats = SkillStats(self._stats_path())

        logger.info(
            "[SkillManager] loaded %d skills from %s | categories=%s",
            len(self.skills.get("all_skills", [])),
            skills_dir,
            dict(self._category_counts()),
        )

    # ------------------------------------------------------------------ #
    # Stats                                                                #
    # ------------------------------------------------------------------ #

    def _stats_path(self) -> str:
        return os.path.join(self._skills_dir, _STATS_FILENAME)

    def record_injection(self, skill_names: list[str]) -> None:
        """Record that these skills were injected into a request."""
        self._stats.record_injection(skill_names)

    def record_feedback(self, skill_names: list[str], score: float) -> None:
        """Record feedback for skills injected in a turn."""
        self._stats.record_feedback(skill_names, score)

    def get_effectiveness(self, skill_name: str) -> float:
        """Effectiveness score for a skill (default ``0.5`` for unknown)."""
        return self._stats.effectiveness(skill_name)

    def flush_stats(self) -> None:
        """Persist any buffered stats mutations to disk."""
        self._stats.save()

    # ------------------------------------------------------------------ #
    # Loading                                                              #
    # ------------------------------------------------------------------ #

    def _load_skills(self) -> dict[str, Any]:
        """Scan ``skills_dir`` for ``*/SKILL.md`` and parse into a flat list."""
        result: dict[str, Any] = {"all_skills": []}
        paths = self._skill_md_paths()
        if not paths:
            logger.warning("[SkillManager] no SKILL.md files found in %s", self._skills_dir)
            return result
        for path in paths:
            skill = frontmatter.parse_skill_md(path)
            if skill is not None:
                result["all_skills"].append(skill)
        return result

    def _skill_md_paths(self) -> list[str]:
        return layout.skill_md_paths(self._skills_dir)

    def _compute_skills_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        fingerprint: list[tuple[str, int, int]] = []
        for path in self._skill_md_paths():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            fingerprint.append((os.path.realpath(path), int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(fingerprint)

    def reload(self) -> None:
        """Re-scan the skills directory and rebuild the internal skill bank."""
        self._stats.reload()
        self.skills = self._load_skills()
        self._skills_fingerprint = self._compute_skills_fingerprint()
        logger.info("[SkillManager] reloaded skills from %s", self._skills_dir)

    def refresh_if_changed(self) -> bool:
        """Reload skills if the on-disk skill library changed externally."""
        current = self._compute_skills_fingerprint()
        if current == self._skills_fingerprint:
            return False
        self.reload()
        self.generation += 1
        logger.info("[SkillManager] detected local skill changes; refreshed library")
        return True

    # ------------------------------------------------------------------ #
    # Skill catalog (injection)                                            #
    # ------------------------------------------------------------------ #

    def get_all_skills(self) -> list[dict]:
        """Return all loaded skills eligible for model invocation.

        Skills with ``disable-model-invocation: true`` in their frontmatter are
        filtered out.
        """
        return [
            s
            for s in self.skills.get("all_skills", [])
            if not s.get("_extra_frontmatter", {}).get("disable-model-invocation", False)
        ]

    def get_skill_path_map(self) -> dict[str, dict[str, str]]:
        """Map every bundle file path → ``{skill_id, skill_name}``.

        Used by the proxy to resolve which skill a ``read`` tool call targets.
        """
        path_map: dict[str, dict[str, str]] = {}
        for s in self.get_all_skills():
            skill_dir = os.path.dirname(str(s.get("file_path", "") or ""))
            bundle_paths = (list_skill_bundle_paths(skill_dir) if skill_dir else []) or ["SKILL.md"]
            public_path = self._public_skill_path(s)
            public_dir = os.path.dirname(public_path) if public_path else ""
            for rel_path in bundle_paths:
                locations = []
                if skill_dir:
                    locations.append(os.path.realpath(os.path.join(skill_dir, rel_path)))
                if public_dir:
                    locations.append(os.path.realpath(os.path.join(public_dir, rel_path)))
                for fp in locations:
                    if fp:
                        path_map[fp] = {"skill_id": s.get("id", ""), "skill_name": s.get("name", "")}
        return path_map

    def _public_skill_path(self, skill: dict) -> str:
        if not self._public_skill_root:
            return ""
        name = str(skill.get("name", "")).strip()
        if not name:
            return ""
        return os.path.join(self._public_skill_root, name, "SKILL.md")

    def _location_for(self, skill: dict) -> str:
        return self._public_skill_path(skill) or skill.get("file_path", "")

    def build_injection_prompt(self, max_chars: int = 30_000, read_tool_name: str = "read") -> str:
        """Catalog all skills and wrap them with the mandatory-skills block.

        Uses the full catalog when it fits within *max_chars*, else the compact
        catalog. Returns the empty string when no skills are loaded.
        """
        return prompt.build_injection_prompt(
            self.get_all_skills(),
            self._location_for,
            max_chars=max_chars,
            read_tool_name=read_tool_name,
        )

    # ------------------------------------------------------------------ #
    # Introspection                                                        #
    # ------------------------------------------------------------------ #

    def _category_counts(self) -> Counter:
        return Counter(str(s.get("category") or "general") for s in self.skills.get("all_skills", []))

    def get_skill_count(self) -> dict:
        return {
            "total": len(self.skills.get("all_skills", [])),
            "by_category": dict(self._category_counts()),
        }
