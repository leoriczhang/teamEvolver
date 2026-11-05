# ruff: noqa: F401
"""Worksheet labels must not consume model repair budgets or hide missing business rows."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import source_unit_extraction as extraction
from team_miner.skill_miner.extract_recovery import prepare_recovery
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.source_unit_extraction import ExactExtractOwner, assignment, output_for


def without_structure(packet):
    value = output_for(packet)
    value["facts"] = []
    refs = packet["derived_controls"]["allowed_source_refs"]
    value["facts"] = [{"text": "原表列名", "source_refs": [ref]}
                      for ref, unit in zip(refs, packet["evidence"], strict=True) if unit["kind"] == "table_header"]
    return value

class SheetOwner(ExactExtractOwner):
    def new(self, count=1):
        job, entries = super().new(count)
        for entry in entries:
            from pathlib import Path

            path = Path(entry["local_path"])
            text = path.read_text().replace("# 方法 0", "## Sheet1")
            path.write_text(text)
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries

async def old_failure(owner, monkeypatch):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from tests.miner.skill_miner.support.source_unit_extraction import POLICY

    job, entries = owner.new(1)
    job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
    for _ in range(3):
        root, state = await owner._staged_load(job)
        current = state["current"]
        row = next(row for row in _read(root / current["packet_file"]) if row["context"]["slot"] == 1)
        packet = _read(root / row["local_path"])
        uri = current["output_uri"] + "/" + row["output_path"]
        output = json.loads(owner.ov.contents[uri])
        neutral = {ref for ref, unit in zip(row["context"]["source_refs"], packet["evidence"], strict=True)
                   if extraction.neutral_structure(unit)}
        output["methods"][0]["source_refs"] = [ref for ref in row["context"]["source_refs"] if ref not in neutral]
        owner.ov.contents[uri] = json.dumps(output, ensure_ascii=False)
        with monkeypatch.context() as old:
            old.setattr(extraction, "neutral_structure", lambda _: False)
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert job["status"] == "failed", job.get("error")
    job = owner.repo.update_job(job["job_id"], ov_status="completed")
    root, state = await owner._staged_load(job)
    return job, root, state
