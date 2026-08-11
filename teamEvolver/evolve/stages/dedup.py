"""
LLM-based semantic dedup gate for newly created skills.

When enabled, this runs after a brand-new skill candidate is generated but
before it is published. Unlike the same-name conflict check, this catches
*differently named* skills whose purpose/content substantially overlaps an
existing skill in the library, so the library does not accumulate redundant
near-duplicates.

Progressive disclosure (to keep context bounded on large libraries):
  Stage 1 - shortlist: the model only sees lightweight metadata (name +
    description) for every existing skill and the candidate, and returns at
    most a handful of names whose purpose *might* overlap the candidate.
  Stage 2 - verdict: only the shortlisted skills have their full content
    fetched and compared against the candidate to produce the final verdict
    with a similarity score, the most similar skill, a reason, and a
    recommended action.

This avoids loading the full text of the entire library into a single prompt.

Failure policy (fail-open): if an LLM call fails or returns invalid JSON, the
candidate is treated as NOT redundant so a transient infra error never blocks
a genuinely new skill. The downstream verifier and validation gates still
apply.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from ..kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Stage 1 may scan a large library, but only with cheap metadata.
_MAX_METADATA = 1000
_METADATA_DESC_MAX_CHARS = 1500
# Stage 2 only fetches content for a small shortlist.
_SHORTLIST_SIZE = 6
_CANDIDATE_CONTENT_MAX_CHARS = 30000
_EXISTING_CONTENT_MAX_CHARS = 7000

# A callable that fetches full content for the given skill names. Returns a
# mapping of name -> content. Used only for the (small) stage-2 shortlist.
FetchContents = Callable[[list[str]], Awaitable[dict[str, str]]]


_SHORTLIST_SYSTEM = """\
You are the first-pass filter of the redundancy gate for teamEvolver skill
creation.

You are given:
- a CANDIDATE new skill (name, description)
- a list of EXISTING skills already in the library (name + description only)

Your ONLY job is to shortlist the existing skills whose PURPOSE / triggering
situation might substantially overlap the candidate, so they can be examined
in detail later. Judge by purpose, not by wording or name similarity.

Be inclusive but focused: return the few most likely overlaps (at most the
requested number), ordered most-likely first. If nothing plausibly overlaps,
return an empty list.

Output EXACTLY one JSON object with:
- "candidates": array of existing skill names (strings) to examine in detail

No markdown fences. No extra text.
"""


_VERDICT_SYSTEM = """\
You are the redundancy gate for teamEvolver skill creation.

You are given:
- a CANDIDATE new skill (name, description, category, content)
- a small list of EXISTING skills (name, description, category, content) that
  were pre-selected as the most likely overlaps

Your job is to decide whether the candidate is a redundant near-duplicate of
some existing skill, or whether it is genuinely distinct and worth adding.

Two skills are REDUNDANT when they target the same purpose / triggering
situation and the candidate would not add materially new, reusable,
environment-specific knowledge beyond what an existing skill already covers
(even if they have different names or wording).

Two skills are DISTINCT when the candidate covers a different capability,
domain, API surface, or procedure, OR adds substantial non-overlapping
knowledge that does not fit naturally into any single existing skill.

Be conservative about declaring redundancy: only do so when the overlap is
clear. Different names alone do NOT make skills distinct, and superficial
wording differences do NOT make them distinct.

Output EXACTLY one JSON object with:
- "decision": "redundant" or "distinct"
- "similarity": number in [0, 1] (max semantic overlap with any existing skill)
- "most_similar_skill": the name of the closest existing skill, or "" if none
- "reason": short explanation of the verdict, citing the overlap or the
  distinguishing knowledge
- "recommended_action": one of "create" (add as new), "merge_into_existing"
  (fold into the most similar skill), or "skip" (drop, fully covered already)

No markdown fences. No extra text.
"""


def _clip_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = str(text or "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    if not clean:
        return None
    try:
        obj = json.loads(clean)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    match = _JSON_BLOCK_RE.search(clean)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_similarity(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    score = max(0.0, min(1.0, float(value)))
    return round(score, 3)


def _build_metadata_payload(existing_skills: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (lightweight metadata list, index by name) for stage 1."""
    payload: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for skill in existing_skills[:_MAX_METADATA]:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name", "") or "").strip()
        if not name:
            continue
        index[name] = skill
        payload.append(
            {
                "name": name,
                "description": _clip_text(skill.get("description", ""), _METADATA_DESC_MAX_CHARS),
            }
        )
    return payload, index


def _disabled_result(reason: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "is_redundant": False,
        "decision": "distinct",
        "similarity": None,
        "most_similar_skill": "",
        "reason": reason,
        "recommended_action": "create",
        "shortlist": [],
    }


async def _shortlist_candidates(
    llm: AsyncLLMClient,
    skill: dict[str, Any],
    metadata: list[dict[str, Any]],
    valid_names: set[str],
    *,
    limit: int,
) -> Optional[list[str]]:
    """Stage 1: pick the few existing skills worth a detailed comparison.

    Returns the shortlisted names (possibly empty) or None on LLM failure.
    """
    payload = {
        "candidate_skill": {
            "name": str(skill.get("name", "")),
            "description": str(skill.get("description", "")),
        },
        "existing_skills": metadata,
        "max_candidates": limit,
    }
    try:
        raw = await llm.chat(
            [
                {"role": "system", "content": _SHORTLIST_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            max_tokens=400,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("[SkillDedup] shortlist call failed for '%s': %s", skill.get("name", ""), exc)
        return None

    parsed = _extract_json_object(raw)
    if not parsed:
        logger.warning("[SkillDedup] invalid shortlist output for '%s'", skill.get("name", ""))
        return None

    raw_names = parsed.get("candidates")
    if not isinstance(raw_names, list):
        return []

    shortlist: list[str] = []
    for item in raw_names:
        name = str(item or "").strip()
        if name in valid_names and name not in shortlist:
            shortlist.append(name)
        if len(shortlist) >= limit:
            break
    return shortlist


async def check_skill_redundancy(
    llm: AsyncLLMClient,
    skill: dict[str, Any],
    existing_skills: list[dict[str, Any]],
    *,
    max_similarity: float = 0.8,
    fetch_contents: Optional[FetchContents] = None,
    shortlist_size: int = _SHORTLIST_SIZE,
) -> dict[str, Any]:
    """Decide whether a new candidate skill is a redundant near-duplicate.

    Uses progressive disclosure: a lightweight metadata pass shortlists likely
    overlaps, then only those have their full content compared. ``existing_skills``
    only needs ``name``/``description``/``category``; full content is loaded for
    the shortlist via ``fetch_contents`` (falling back to any ``content`` already
    present on the metadata entries).

    Returns a verdict dict with ``is_redundant`` plus the LLM's reason,
    similarity score, most similar existing skill, and recommended action.
    Fails open (``is_redundant=False``) on any error.
    """
    threshold = max(0.0, min(1.0, float(max_similarity)))
    limit = max(1, int(shortlist_size or 1))

    metadata, index = _build_metadata_payload(existing_skills)
    if not metadata:
        return _disabled_result("No existing skills to compare against.")

    # Stage 1: shortlist by metadata only.
    shortlist = await _shortlist_candidates(
        llm, skill, metadata, set(index), limit=limit
    )
    if shortlist is None:
        return _disabled_result("Dedup shortlist failed, allowing creation.")
    if not shortlist:
        result = _disabled_result("No existing skill plausibly overlaps the candidate.")
        result["shortlist"] = []
        return result

    # Stage 2: fetch content only for the shortlist, then judge.
    contents: dict[str, str] = {}
    if fetch_contents is not None:
        try:
            fetched = await fetch_contents(shortlist)
            if isinstance(fetched, dict):
                contents = {str(k): str(v or "") for k, v in fetched.items()}
        except Exception as exc:
            logger.warning("[SkillDedup] content fetch failed for shortlist %s: %s", shortlist, exc)

    existing_payload: list[dict[str, Any]] = []
    for name in shortlist:
        meta = index.get(name, {})
        content = contents.get(name) or str(meta.get("content", "") or "")
        existing_payload.append(
            {
                "name": name,
                "description": str(meta.get("description", "") or ""),
                "category": str(meta.get("category", "general") or "general"),
                "content": _clip_text(content, _EXISTING_CONTENT_MAX_CHARS),
            }
        )

    payload = {
        "candidate_skill": {
            "name": str(skill.get("name", "")),
            "description": str(skill.get("description", "")),
            "category": str(skill.get("category", "general")),
            "content": _clip_text(skill.get("content", ""), _CANDIDATE_CONTENT_MAX_CHARS),
        },
        "existing_skills": existing_payload,
        "redundancy_similarity_threshold": round(threshold, 3),
    }

    try:
        raw = await llm.chat(
            [
                {"role": "system", "content": _VERDICT_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            max_tokens=1500,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("[SkillDedup] verdict call failed for '%s': %s", skill.get("name", ""), exc)
        return _disabled_result(f"Dedup verdict failed, allowing creation: {exc}")

    parsed = _extract_json_object(raw)
    if not parsed:
        logger.warning("[SkillDedup] invalid verdict output for '%s'", skill.get("name", ""))
        return _disabled_result("Dedup returned invalid JSON, allowing creation.")

    similarity = _normalize_similarity(parsed.get("similarity"))
    decision_raw = str(parsed.get("decision", "") or "").strip().lower()
    reason = str(parsed.get("reason") or parsed.get("rationale") or "").strip()
    most_similar = str(parsed.get("most_similar_skill") or "").strip()
    recommended = str(parsed.get("recommended_action", "") or "").strip().lower()
    if recommended not in {"create", "merge_into_existing", "skip"}:
        recommended = ""

    is_redundant = decision_raw == "redundant"
    # The similarity threshold can independently force a redundant verdict so a
    # high-overlap candidate is blocked even if the model hedged its decision.
    if similarity is not None and similarity >= threshold:
        is_redundant = True
    if decision_raw not in {"redundant", "distinct"}:
        is_redundant = similarity is not None and similarity >= threshold

    if not recommended:
        recommended = "merge_into_existing" if is_redundant else "create"

    return {
        "enabled": True,
        "is_redundant": bool(is_redundant),
        "decision": "redundant" if is_redundant else "distinct",
        "similarity": similarity,
        "threshold": round(threshold, 3),
        "most_similar_skill": most_similar,
        "reason": reason,
        "recommended_action": recommended,
        "shortlist": shortlist,
    }
