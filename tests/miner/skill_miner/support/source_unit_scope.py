# ruff: noqa: F401
"""Exact routing preserves structural context without turning it into method instructions."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner.ov_case_batch_orchestration import case_source_roles
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_unit_extraction import (
    bind_source_unit_index,
    build_source_unit_packet,
    next_source_unit_assignment,
)
from team_miner.skill_miner.source_unit_scope import (
    exact_candidate_scope,
    exact_case_groups,
    exact_method_groups,
    route_exact_source_candidates,
)
from team_miner.skill_miner.source_units import load_source_unit, load_source_units
from tests.miner.skill_miner.support.source_unit_extraction import POLICY, output_for
from tests.miner.skill_miner.support.source_units import frozen


def route_fixture(root, *, row_count=2, max_units=16, uncertain=False, empty_file=False, extra_source=None):
    text = "# 完整复核方法\n\n| 条件 | 方法 |\n| --- | --- |\n" + "".join(
        f"| 条件 {index} | 先核实输入，再按该条件确认结果。 |\n" for index in range(row_count)
    ) + "\n"
    sources = [text, extra_source] if extra_source is not None else [text, ""] if empty_file else [text]
    state, manifest = frozen(root, sources)
    cursor, count, method_count = 0, 0, 0
    while cursor < manifest["unit_count"]:
        context = next_source_unit_assignment(root, cursor=cursor, count=manifest["unit_count"],
                                              unit_index=count, language_policy=POLICY, max_units=max_units)
        packet = build_source_unit_packet(root, context, POLICY)
        value = output_for(packet)
        value["context_uncertain"] = uncertain
        value["context_refs"] = [ref for ref, unit in zip(context["source_refs"], packet["evidence"], strict=True)
                                 if unit["kind"] in {"heading", "table_header", "separator", "empty"}]
        accepted = bind_source_unit_index(root, value, packet, context, f"child::{count}")
        _save(root / "units/0" / f"{count:08d}.json", accepted)
        for method in accepted["value"]["methods"]:
            item_id = f"method-{method_count}"
            _save(root / "catalog/method" / f"{method_count:08d}.json", {
                "item": {**method, "item_id": item_id},
                "source_unit_bindings": accepted["source_unit_bindings"],
            })
            _save(root / "method-decisions" / (item_id + ".json"), {"target": "candidate", "name": "review"})
            method_count += 1
        count += 1
        cursor = context["after"]
    _save(root / "catalog/manifest.json", {"method": method_count})
    _save(root / "plan.json", {"skills": [{"name": "review", "task": "复核方法", "boundary": "按原文条件"}]})
    _save(root / "scopes/review.json", {"ordinals": list(range(state["chunk_count"]))})
    state.update(unit_count=count, source_unit_count=manifest["unit_count"])
    audit = route_exact_source_candidates(root, state, "viking://resources")
    return _read(root / "scopes/review.json"), audit, state
