# ruff: noqa: F401
"""Inert tests for source-ID citations, complete coverage and one audited protocol upgrade."""

from __future__ import annotations

import asyncio
import copy
import tracemalloc

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy
from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner import evidence_quote_units as units
from tests.miner.skill_miner.support.deferred_evidence_review import LANGUAGE, Host, response, run_review, seed


def id_response(packet, outcome="candidate", *, selected=None, reason="Original rules support this procedure."):
    value = response(packet, outcome, reason=reason)
    value["schema_version"] = units.QUOTE_SCHEMA
    row = value["reviews"][0]
    row.pop("supporting_quotes")
    row["supporting_quote_ids"] = [packet["evidence"][0]["id"]] if selected is None else selected
    if not row["supporting_quote_ids"]:
        row["partial_steps"] = []
    else:
        row["partial_steps"] = [{"text": "Retain the original decision condition.", "quote_indexes": [0]}]
    return value

def packet_for(original):
    evidence = [units.source_quote_unit(original, index) for index in range(units.quote_unit_count(original))]
    return {
        "input_ids": [unit["id"] for unit in evidence],
        "evidence": evidence,
        "derived_controls": {"items": [{"item_id": "method-01"}], "allowed_source_refs": [original["source_uri"]]},
    }

class UpgradeHost(Host):
    def __init__(self, *, fresh_stop=False):
        super().__init__()
        self.fresh_stop = fresh_stop
        self.row = {"job_id": "test-job", "status": "running"}

    def _ov_skill_mining_repository(self):
        return self

    def get_job(self, job_id):
        return {**self.row, "status": "stopping"} if self.fresh_stop else dict(self.row)

    def update_job(self, job_id, **changes):
        self.row.update(changes)
        return dict(self.row)

def failed_legacy_current(tmp_path, state, host, monkeypatch):
    monkeypatch.setattr(
        compile_strategy,
        "render_single_stage_compiler",
        lambda base, stage, rules: "---\nname: inert-legacy-review\ndescription: Inert legacy test\n---\n" + rules,
    )
    monkeypatch.setattr(compile_strategy, "compiler_business_policy", lambda base: {"common": "", "stages": {}})
    job = dict(host.row)
    asyncio.run(host._staged_evidence_review_next(job, tmp_path, state))
    current = host.current
    folder = tmp_path / "children" / current["child"]
    request, compiler = {"from": ["viking://frozen/input"], "to": "viking://frozen/output"}, "frozen legacy compiler"
    review._save(folder / "request.json", request)
    (folder / "compiler.md").write_text(compiler, encoding="utf-8")
    raw = review._json(response(host.current_packet, quote="Invented unsupported refund rule."))
    (folder / "result.txt").write_text(raw, encoding="utf-8")
    error = "Supporting quote 0 must literally occur in current original Evidence"
    current.update(
        attempt=2,
        task_id="completed-old-native-task",
        request_file=str((folder / "request.json").relative_to(tmp_path)),
        compiler_file=str((folder / "compiler.md").relative_to(tmp_path)),
        request_sha256=review._sha(review._json(request)),
        compiler_sha256=review._sha(compiler),
        context={**current["context"], "repair_error": error},
    )
    state["current"] = copy.deepcopy(current)
    predecessor = {
        **current,
        "child": "legacy-attempt-1",
        "attempt": 1,
        "context": {**current["context"], "repair_error": error},
    }
    review._save(tmp_path / "children/legacy-attempt-1/metadata.json", predecessor)
    immutable = {path: path.read_bytes() for path in folder.iterdir()}
    return job, error, immutable
