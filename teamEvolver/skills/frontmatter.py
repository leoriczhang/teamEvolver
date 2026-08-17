"""SKILL.md YAML frontmatter parsing.

Single source of truth for reading the AgentSkills-compatible ``SKILL.md``
frontmatter. Used by the skill manager (loading local skills) and the skill
hub (enriching manifest entries), so category resolution stays consistent.

``SKILL.md`` layout::

    ---
    name: debug-systematically
    description: "Use when diagnosing a bug. NOT for: simple typo fixes."
    metadata:
      { "teamEvolver": { "category": "coding" } }
    ---

    # Debug Systematically
    ...

Category resolution order:
  1. ``metadata.<METADATA_NAMESPACE>.category``
  2. top-level ``category`` (legacy)
  3. ``"general"`` (default)
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Persisted metadata namespace inside SKILL.md frontmatter. This string is
# written into on-disk and cloud-shared skill bundles, so it is a wire
# constant: renaming it would drop the category of every existing skill.
METADATA_NAMESPACE = "teamEvolver"

# Frontmatter keys owned by the parser; everything else is preserved verbatim.
_CORE_FM_KEYS = {"name", "description", "metadata", "category"}


def _split_frontmatter(raw: str) -> Optional[tuple[str, str]]:
    """Split raw ``SKILL.md`` text into ``(frontmatter, body)``.

    Returns ``None`` when the document has no leading ``---`` frontmatter block.
    """
    if not raw.startswith("---"):
        return None
    end_idx = raw.find("\n---", 3)
    if end_idx == -1:
        return None
    return raw[3:end_idx].strip(), raw[end_idx + 4 :].strip()


def _load_frontmatter(path: str) -> Optional[dict[str, Any]]:
    """Read *path* and return its parsed frontmatter dict (or ``None``)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("[frontmatter] could not read %s: %s", path, e)
        return None
    return _load_frontmatter_from_raw(raw, path)


def _load_frontmatter_from_raw(raw: str, path: str = "") -> Optional[dict[str, Any]]:
    split = _split_frontmatter(raw)
    if split is None:
        return None
    fm_text, _body = split
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        logger.warning("[frontmatter] invalid YAML frontmatter in %s", path or "<memory>")
        fm = {}
    return fm if isinstance(fm, dict) else None


def resolve_category(fm: dict[str, Any]) -> Optional[str]:
    """Resolve a skill's category from its frontmatter dict.

    Returns ``None`` when neither namespaced nor legacy category is present,
    letting callers apply their own default.
    """
    metadata = fm.get("metadata")
    ns_meta = (metadata or {}).get(METADATA_NAMESPACE, {}) if isinstance(metadata, dict) else {}
    if isinstance(ns_meta, dict) and ns_meta.get("category"):
        return str(ns_meta["category"]).strip()
    if fm.get("category"):
        return str(fm["category"]).strip()
    return None


def parse_skill_md(path: str) -> Optional[dict[str, Any]]:
    """Parse a ``SKILL.md`` file into a skill dict.

    Returns keys ``id``/``name``/``description``/``category``/``content``/
    ``file_path`` plus optional ``metadata`` and ``_extra_frontmatter`` (extra
    frontmatter fields preserved verbatim for round-trip). Returns ``None`` when
    the file lacks frontmatter or required ``name``/``description`` fields.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("[frontmatter] could not read %s: %s", path, e)
        return None

    split = _split_frontmatter(raw)
    if split is None:
        return None
    fm_text, body = split

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        logger.warning("[frontmatter] invalid YAML frontmatter in %s", path)
        fm = {}
    if not isinstance(fm, dict):
        return None

    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()
    if not name or not description:
        logger.warning("[frontmatter] skipping %s — missing name or description", path)
        return None

    metadata = fm.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        metadata = None

    result: dict[str, Any] = {
        "id": hashlib.sha256(name.encode()).hexdigest()[:12],
        "name": name,
        "description": description,
        "category": resolve_category(fm) or "general",
        "content": body,
        "file_path": os.path.realpath(path),
    }
    if metadata:
        result["metadata"] = metadata

    extra = {k: v for k, v in fm.items() if k not in _CORE_FM_KEYS}
    if extra:
        result["_extra_frontmatter"] = extra

    return result


def parse_skill_md_text(raw: str) -> Optional[dict[str, str]]:
    """Parse ``SKILL.md`` *text* into ``{name, description}`` (or ``None``).

    A text-only counterpart to :func:`parse_skill_md` for callers that already
    hold the document body (e.g. a resolved OpenViking read) and must derive a
    skill's identity without touching disk. Returns ``None`` when the document
    lacks frontmatter or the required ``name``/``description`` fields.
    """
    split = _split_frontmatter(str(raw or ""))
    if split is None:
        return None
    fm_text, _body = split
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()
    if not name or not description:
        return None
    return {"name": name, "description": description}


def enrich_manifest_entry(entry: dict[str, Any], skill_path: str) -> None:
    """Fill ``description``/``category`` in a manifest *entry* from ``SKILL.md``.

    Mutates *entry* in place; silently no-ops when the file cannot be read or
    has no frontmatter.
    """
    fm = _load_frontmatter(skill_path)
    if fm is None:
        return

    desc = fm.get("description")
    if desc:
        entry["description"] = str(desc).strip()

    category = resolve_category(fm)
    if category:
        entry["category"] = category
