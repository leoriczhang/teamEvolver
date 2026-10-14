"""Integrate source batches into one method while retaining every accepted revision for audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from team_skills.library.frontmatter import render_skill_md

from . import bounded_compile_plan as planning

PROTOCOL = "te-incremental-skill-body.v1"
RULE = """Maintain ONE complete reusable method for the assigned candidate. The previous_body control is an
unreviewed generated working copy from this SAME candidate and round, not new Evidence. Read the current raw
Evidence and integrate its relevant additions into that copy. Output the entire updated method, never append
another complete version for a persona, source or scenario. Keep one procedure with conditional branches and
parameterized examples. Preserve earlier supported steps, conditions, exceptions, source citations and unresolved
conflicts; do not silently discard them when compressing. Current raw Evidence may correct an earlier draft error;
explain conflicting source claims instead of choosing one as fact. Optional steps stay optional, added durations
stay additions, and consent prerequisites stay attached to the corresponding action. Example amounts, names and
company facts are parameters of their stated scenarios, not universal rules. Preserve the source task boundary;
illustrative or training material must not become authorization for real-world purchase or eligibility decisions.
Do not copy long dialogues, invent steps from reference tables, or extend the candidate into neighboring methods.
Prefer a concise method organized by trigger, inputs, ordered actions/branches, completion and limits; detail must
serve execution. Return skill-miner.section.v1 with exactly schema_version, input_ids (ONLY all CURRENT assigned
raw chunk IDs), a short method title and complete Markdown content without frontmatter, at most 10000 characters.
The complete JSON must fit 12000 characters. If the CURRENT batch adds no applicable rule or correction, return
skill-miner.section-skip.v1 with exactly schema_version, input_ids and reason; TE retains the previous working copy.
This skip records inspected current scope, never a whole-method Evidence gap. Every source batch remains archived;
the final Skill contains the latest integrated method, not the concatenation of working copies."""


def previous_body(root: Path, name: str, section_count: int) -> dict:
    """Use the latest operational revision, retaining skipped batches in the coverage ledger."""
    for index in range(section_count - 1, -1, -1):
        path = root / "sections" / name / f"{index:08d}.json"
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Skill working copy must stay inside its frozen round")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        if value.get("schema_version") == planning.SECTION_SKIP_SCHEMA:
            planning.validate_section_skip(value, input_ids=value.get("input_ids", []))
            continue
        planning.validate_section(value, input_ids=value.get("input_ids", []))
        return {"section_index": index, "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "title": value["title"], "content": value["content"], "is_evidence": False}
    return {"section_index": None, "sha256": "", "title": "", "content": "", "is_evidence": False}


def iter_integrated_skill(candidate: dict, sections, *, expected_input_ids: list[str], source_refs: list[str]):
    """Coverage includes every intermediate revision and skip; only the latest body is rendered."""
    if not expected_input_ids or len(set(expected_input_ids)) != len(expected_input_ids):
        raise ValueError("An integrated Skill needs exact unique source coverage")
    if (not source_refs or len(source_refs) != len(set(source_refs))
            or any(not isinstance(ref, str) or not ref.startswith("viking://") for ref in source_refs)):
        raise ValueError("An integrated Skill needs exact original source references")
    covered, latest = set(), None
    for section in sections:
        ids = section.get("input_ids", [])
        if section.get("schema_version") == planning.SECTION_SKIP_SCHEMA:
            planning.validate_section_skip(section, input_ids=ids)
        else:
            planning.validate_section(section, input_ids=ids)
            latest = section
        if covered.intersection(ids):
            raise ValueError("Integrated Skill source batches overlap")
        covered.update(ids)
    if covered != set(expected_input_ids):
        raise ValueError("Integrated Skill source coverage is incomplete")
    if latest is None:
        raise planning.NoOperationalSkillSectionsError("No operational method across the inspected source scope")
    label = "来源" if any("\u4e00" <= char <= "\u9fff" for char in latest["title"]) else "Sources"
    references = "\n".join("- " + ref for ref in source_refs)
    yield render_skill_md(name=candidate["name"], description=candidate["task"],
                          content=f"# {latest['title']}\n\n{latest['content']}\n\n## {label}\n\n{references}")
