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
import re
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Persisted metadata namespace inside SKILL.md frontmatter. This string is
# written into on-disk and cloud-shared skill bundles, so it is a wire
# constant: renaming it would drop the category of every existing skill.
METADATA_NAMESPACE = "teamEvolver"

# Frontmatter keys owned by the parser; everything else is preserved verbatim.
_CORE_FM_KEYS = {"name", "description", "metadata", "category"}
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


def _split_frontmatter(raw: str) -> Optional[tuple[str, str]]:
    """Split raw ``SKILL.md`` text into ``(frontmatter, body)``.

    Returns ``None`` when the document has no leading ``---`` frontmatter block.
    """
    match = _FRONTMATTER_RE.match(str(raw or ""))
    if match is None:
        return None
    return match.group("header").strip(), raw[match.end() :].strip()


def split_skill_md(raw: str, *, strict: bool = False) -> Optional[tuple[str, str]]:
    """Split a SKILL.md document and optionally reject malformed delimiters."""
    text = str(raw or "")
    split = _split_frontmatter(text)
    if split is None and strict and text.startswith("---"):
        raise ValueError("SKILL.md has an unterminated YAML frontmatter block")
    return split


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate_skill_md(
    raw: bytes | str,
    *,
    expected_name: str = "",
) -> dict[str, Any]:
    """Validate the strict, upclaw-compatible SKILL.md wire format."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md must be UTF-8") from exc
    split = split_skill_md(text, strict=True)
    if split is None:
        raise ValueError("SKILL.md must start with YAML frontmatter")
    header, body = split
    try:
        values = yaml.safe_load(header) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc
    if not isinstance(values, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    name = str(values.get("name") or "").strip()
    description = str(values.get("description") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter requires name")
    if expected_name and name != expected_name:
        raise ValueError(
            f"SKILL.md name {name!r} does not match expected {expected_name!r}"
        )
    if not description:
        raise ValueError("SKILL.md frontmatter requires description")
    header_lines = header.splitlines()
    description_index = next(
        (
            index
            for index, line in enumerate(header_lines)
            if re.match(r"^description\s*:", line)
        ),
        -1,
    )
    if description_index < 0 or not header_lines[description_index].split(":", 1)[1].strip():
        raise ValueError("description must be encoded on one physical line")
    if (
        description_index + 1 < len(header_lines)
        and header_lines[description_index + 1].startswith((" ", "\t"))
    ):
        raise ValueError("description must not use a folded YAML continuation")
    if body.startswith("---"):
        raise ValueError("SKILL.md body contains a second leading frontmatter block")
    return {"frontmatter": values, "body": body}


def render_skill_md(
    *,
    name: Any,
    description: Any,
    category: Any = "general",
    content: Any = "",
    extra_frontmatter: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    """Render the canonical, single-frontmatter SKILL.md representation."""
    normalized_name = str(name or "").strip()
    normalized_description = _single_line(description)
    normalized_category = str(category or "general").strip() or "general"
    if not normalized_name:
        raise ValueError("skill name must not be empty")
    if not normalized_description:
        raise ValueError("skill description must not be empty")

    body = str(content or "")
    embedded: dict[str, Any] = {}
    if body.startswith("---"):
        split = split_skill_md(body, strict=True)
        if split is None:
            raise ValueError("invalid embedded SKILL.md frontmatter")
        embedded_header, body = split
        try:
            parsed = yaml.safe_load(embedded_header) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"embedded SKILL.md frontmatter is invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("embedded SKILL.md frontmatter must be a mapping")
        embedded = {
            key: value
            for key, value in parsed.items()
            if key not in {"name", "description", "category"}
        }

    frontmatter_values: dict[str, Any] = {
        **embedded,
        **dict(extra_frontmatter or {}),
        "name": normalized_name,
        "description": normalized_description,
        "category": normalized_category,
    }
    if isinstance(metadata, dict) and metadata:
        frontmatter_values["metadata"] = metadata
    header = yaml.safe_dump(
        frontmatter_values,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=2_147_483_647,
    ).strip()
    rendered = f"---\n{header}\n---\n\n{body.strip()}\n"
    validate_skill_md(rendered, expected_name=normalized_name)
    return rendered


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


def apply_frontmatter_to_manifest_entry(entry: dict[str, Any], fm: Optional[dict[str, Any]]) -> None:
    """Fill ``description``/``category``/runtime policy on a manifest *entry*.

    Single source of the frontmatter → manifest mapping, shared by the
    file-based (:func:`enrich_manifest_entry`) and in-memory
    (:func:`enrich_manifest_entry_from_raw`) callers. Mutates *entry* in place
    and no-ops on empty frontmatter.
    """
    if not fm:
        return

    desc = fm.get("description")
    if desc:
        entry["description"] = str(desc).strip()

    category = resolve_category(fm)
    if category:
        entry["category"] = category
    portable = fm.get("portable")
    supported = fm.get("supported_runtimes")
    if isinstance(portable, bool) or isinstance(supported, (list, tuple, set)):
        entry["runtime_policy"] = {
            "portable": bool(portable),
            "supported_runtimes": [
                str(item).strip().lower()
                for item in supported or []
                if str(item).strip()
            ],
        }


def enrich_manifest_entry(entry: dict[str, Any], skill_path: str) -> None:
    """Fill ``description``/``category`` in a manifest *entry* from ``SKILL.md``.

    Mutates *entry* in place; silently no-ops when the file cannot be read or
    has no frontmatter.
    """
    apply_frontmatter_to_manifest_entry(entry, _load_frontmatter(skill_path))


def enrich_manifest_entry_from_raw(
    entry: dict[str, Any], raw_md: bytes | str, path: str = ""
) -> None:
    """Same as :func:`enrich_manifest_entry` for in-memory ``SKILL.md`` bytes.

    Used by the hub-only console paths, where the skill never touches disk.
    """
    raw = (
        raw_md.decode("utf-8", errors="replace")
        if isinstance(raw_md, (bytes, bytearray))
        else str(raw_md or "")
    )
    apply_frontmatter_to_manifest_entry(entry, _load_frontmatter_from_raw(raw, path))
