# ruff: noqa: F401
"""Original constraints, boundaries and full source coverage survive precise writer routing."""

import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_staged_orchestration import (
    _alias,
    _bind_unit,
    _canonical_extract_references,
    _freeze_chunks,
    _method_groups,
    _read,
    _save,
    _sha,
)
from team_miner.skill_miner.source_scope import prepare_source_routing, route_candidate_scope, route_final_candidates


def sources(tmp_path, text=None):
    root = tmp_path / "staged"
    entries = []
    originals = text or ["ordinary unrelated material\n" * 12000, "Shared exception: obtain consent.\n"]
    for index, body in enumerate(originals):
        path = tmp_path / f"source-{index}.md"
        path.write_text(body)
        entries.append({"kind": "source", "local_path": str(path), "relative_path": path.name, "sha256": _sha(body)})
    count = _freeze_chunks(root, entries, "viking://resources/frozen")
    state = {"chunk_count": count, "unit_count": count}
    for ordinal in range(count):
        chunk = _read(root / "chunks" / f"{ordinal:08d}.json")
        ref = _alias(chunk["id"])
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"]],
            "methods": [], "facts": [{"text": "Original material", "source_refs": [ref]}], "deferred": [],
            "context_refs": [ref] if ordinal == count - 1 else [], "context_uncertain": False,
        }
        planning.validate_index(value, input_ids=[chunk["id"]], source_refs=[ref], routing=True)
        unit = _bind_unit(value, {ref: [ordinal]}, f"extract-{ordinal}")
        _save(root / "units/0" / f"{ordinal:08d}.json", unit)
    return root, state
