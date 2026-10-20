# ruff: noqa: F401
"""Fresh citation granularity preserves originals, frozen rounds and reviewed artifact dependencies."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import (
    FINE_EXTRACT_MAX_CHUNKS,
    FINE_SOURCE_FRAGMENT_CHARS,
    SOURCE_FRAGMENT_PROTOCOL,
    _alias,
    _bind_unit,
    _freeze_chunks,
    _group_count,
    _raw_group,
    _read,
    _save,
    _sha,
)
from team_miner.skill_miner.round_reuse import (
    PROTOCOL_FIELDS,
    prepare_round_reuse,
    try_reuse_candidate,
    try_reuse_extract,
)
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3, prepare_source_routing, route_candidate_scope
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.selection_window_recovery import RecoveryOwner

PREFIX = "viking://resources/frozen"

def freeze(tmp_path, text, *, budget=FINE_SOURCE_FRAGMENT_CHARS):
    path = tmp_path / "source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    root = tmp_path / "staged"
    entries = [{"kind": "source", "relative_path": "original/source.md", "local_path": str(path),
                "sha256": _sha(text), "content_bytes": len(text.encode())}]
    count = _freeze_chunks(root, entries, PREFIX, max_chars=budget)
    return root, [_read(root / "chunks" / f"{ordinal:08d}.json") for ordinal in range(count)]

def local_routing(root, chunks, offsets):
    anchors = [next(chunk["ordinal"] for chunk in chunks if chunk["start_char"] <= offset < chunk["end_char"])
               for offset in offsets]
    refs = {chunk["ordinal"]: _alias(chunk["id"]) for chunk in chunks}
    methods = set(anchors)
    value = {"schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"] for chunk in chunks],
             "methods": [{"task": f"Supported independent method {index}", "boundary": "Use original conditions",
                          "source_refs": [refs[ordinal]]} for index, ordinal in enumerate(sorted(methods))],
             "facts": [{"text": "Retained original background", "source_refs": [refs[ordinal]]}
                       for ordinal in range(len(chunks)) if ordinal not in methods],
             "deferred": [], "context_refs": [refs[0]], "context_uncertain": False}
    planning.validate_index(value, input_ids=value["input_ids"], source_refs=list(refs.values()), routing=True)
    unit = _bind_unit(value, {ref: [ordinal] for ordinal, ref in refs.items()}, "accepted-original")
    _save(root / "units/0/00000000.json", unit)
    routing = prepare_source_routing(root, {"source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3,
                                          "chunk_count": len(chunks), "unit_count": 1})
    scopes = [route_candidate_scope(root, {"ordinals": list(range(len(chunks))), "method_ordinals": [ordinal]},
                                   routing) for ordinal in anchors]
    return anchors, scopes

class FullRecoveryOwner(RecoveryOwner):
    def __init__(self, root, *, fine=True):
        super().__init__(root)
        self.ov = self.fixture.ov = DirectFake()
        self.fine = fine

    def new(self, count=3):
        job, entries = super().new(count)
        marker = SOURCE_FRAGMENT_PROTOCOL if self.fine else ""
        return self.repo.update_job(job["job_id"], staged_source_fragment_protocol=marker), entries

async def reviewed(owner):
    job, entries = owner.new(3)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return first
