# ruff: noqa: F401
"""Local accepted-unit context preserves original rules without globalizing unrelated workflows."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import source_scope
from team_miner.skill_miner.ov_staged_orchestration import _alias, _bind_unit, _freeze_chunks, _read, _save, _sha
from team_miner.skill_miner.round_reuse import _profile, _stat
from team_miner.skill_miner.source_scope import (
    SOURCE_SCOPE_PROTOCOL,
    SOURCE_SCOPE_PROTOCOL_V3,
    candidate_context_ordinals,
    prepare_source_routing,
    route_candidate_scope,
    route_final_candidates,
)

PREFIX = "viking://resources/frozen"

def local_sources(tmp_path, *, unowned=False, uncertain=False, cross=False):
    root = tmp_path / "staged"
    entries = []
    for index, body in enumerate(("A" * 95000, "B" * 95000)):
        path = tmp_path / f"source-{index}.md"
        path.write_text(body)
        entries.append({"kind": "source", "local_path": str(path), "relative_path": path.name, "sha256": _sha(body)})
    count = _freeze_chunks(root, entries, PREFIX)
    files = [_read(root / "source-files" / (_sha(f"{PREFIX}/source-{index}.md") + ".json"))["ordinals"]
             for index in range(2)]
    first, second = files[0][1], files[1][1]
    context_a, context_b = files[0][-2], files[1][-2]
    if cross:
        context_a, context_b = context_b, context_a
    groups = [([first, context_a], [first], [context_a], uncertain == "owned"),
              ([second, context_b], [second], [context_b], False)]
    assigned = {ordinal for ranges, *_ in groups for ordinal in ranges}
    for ordinal in range(count):
        if ordinal not in assigned:
            marked = ordinal == files[1][-1] and (unowned or uncertain == "unowned")
            groups.append(([ordinal], [], [ordinal] if marked else [], bool(marked and uncertain == "unowned")))
    for index, (ordinals, methods, contexts, unsure) in enumerate(groups):
        chunks = [_read(root / "chunk-metadata" / f"{ordinal:08d}.json") for ordinal in ordinals]
        parents = {_alias(chunk["id"]): [ordinal] for chunk, ordinal in zip(chunks, ordinals)}
        refs = {ordinal: _alias(chunk["id"]) for chunk, ordinal in zip(chunks, ordinals)}
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
            "methods": [{"task": "Review documented inputs", "boundary": "Use the exact original conditions",
                         "source_refs": [refs[ordinal]]} for ordinal in methods],
            "facts": [{"text": "Original reference or constraint", "source_refs": [refs[ordinal]]}
                      for ordinal in ordinals if ordinal not in methods],
            "deferred": [], "context_refs": [refs[ordinal] for ordinal in contexts], "context_uncertain": unsure,
        }
        planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(parents), routing=True)
        _save(root / "units/0" / f"{index:08d}.json", _bind_unit(value, parents, f"extract-{index}"))
    state = {"chunk_count": count, "unit_count": len(groups), "source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3}
    return root, state, files, (first, second), (context_a, context_b)

def scope(files, method):
    return {"ordinals": list(files), "method_ordinals": [method]}
