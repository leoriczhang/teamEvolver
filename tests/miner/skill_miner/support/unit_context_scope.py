# ruff: noqa: F401
"""Uncertain applicability shares assigned original units, without amplifying full files."""

from __future__ import annotations

import copy
import hashlib

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import source_scope
from team_miner.skill_miner.ov_staged_orchestration import (
    _alias,
    _bind_unit,
    _freeze_chunks,
    _method_groups,
    _read,
    _save,
    _sha,
)
from team_miner.skill_miner.source_scope import (
    SOURCE_SCOPE_PROTOCOL_V3,
    UNCERTAINTY_SCOPE_PROTOCOL,
    candidate_context_ordinals,
    prepare_source_routing,
    route_candidate_scope,
    route_final_candidates,
)
from tests.miner.skill_miner.support.source_scope_v3 import PREFIX, local_sources, scope


def enabled(state):
    return {**state, "uncertainty_scope_protocol": UNCERTAINTY_SCOPE_PROTOCOL}

def accept_groups(root, state, assigned):
    """Create exact generic accepted classifications, including every remaining original range."""
    groups = list(assigned)
    seen = {ordinal for ranges, *_ in groups for ordinal in ranges}
    groups.extend(([ordinal], [], [], False) for ordinal in range(state["chunk_count"]) if ordinal not in seen)
    for path in (root / "units/0").glob("*.json"):
        path.unlink()
    for index, (ranges, methods, contexts, unsure) in enumerate(groups):
        chunks = [_read(root / "chunk-metadata" / f"{ordinal:08d}.json") for ordinal in ranges]
        refs = {ordinal: _alias(chunk["id"]) for ordinal, chunk in zip(ranges, chunks)}
        parents = {refs[ordinal]: [ordinal] for ordinal in ranges}
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
            "methods": [{"task": "Review the supplied request", "boundary": "Preserve original conditions",
                         "source_refs": [refs[ordinal]]} for ordinal in methods],
            "facts": [{"text": "Original context", "source_refs": [refs[ordinal]]}
                      for ordinal in ranges if ordinal not in methods],
            "deferred": [], "context_refs": [refs[ordinal] for ordinal in contexts],
            "context_uncertain": unsure,
        }
        planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(parents), routing=True)
        _save(root / "units/0" / f"{index:08d}.json", _bind_unit(value, parents, f"extract-{index}"))
    state["unit_count"] = len(groups)
