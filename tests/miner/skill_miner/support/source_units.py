# ruff: noqa: F401
"""Exact source units preserve source text and semantic boundaries without widening evidence."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_units import (
    PROTOCOL,
    freeze_source_units,
    load_source_unit,
    load_source_units,
    source_units,
)

URI = "viking://resources/original.md"

def chunks_for(text, uri=URI, *, max_chars=800, start=0):
    return [dict(chunk, ordinal=start + index)
            for index, chunk in enumerate(planning.source_chunks(text, uri, max_chars=max_chars))]

def frozen(root, texts, *, max_chars=800):
    chunks = []
    for index, text in enumerate(texts):
        chunks.extend(chunks_for(text, f"viking://resources/source-{index}.md", max_chars=max_chars, start=len(chunks)))
    for chunk in chunks:
        _save(root / "chunks" / f"{chunk['ordinal']:08d}.json", chunk)
    state = {"source_unit_protocol": PROTOCOL, "chunk_count": len(chunks)}
    manifest = freeze_source_units(root, state)
    return state, manifest
