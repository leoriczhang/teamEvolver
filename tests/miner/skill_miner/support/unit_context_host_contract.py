# ruff: noqa: F401
"""Fresh uncertainty-unit routing preserves frozen originals and reviewed-round contracts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_refinement import mapped_previous_source_scope, merge_candidate_scopes
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3, UNCERTAINTY_SCOPE_PROTOCOL
from tests.miner.skill_miner.support.compact_host_contract import CompactFake, CompactOwner
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.fresh_alias_and_case_group_contract import dataset
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class UnitContextFake(CompactFake):
    """Only the first long-document window has uncertain linkage; later text is ordinary notes."""

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract_batch-" not in request["from"][0]:
            return accepted
        prefix = request["from"][0] + "/"
        for uri, text in list(self.contents.items()):
            if not uri.startswith(prefix):
                continue
            packet = json.loads(text)
            if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                continue
            controls = packet["derived_controls"]
            refs = {identity: ref for ref, identity in controls["reference_map"].items()}
            target = request["to"] + "/" + packet["output_path"]
            value = json.loads(self.contents[target])
            value.update(methods=[], facts=[], deferred=[], context_refs=[], context_uncertain=False)
            for chunk in packet["evidence"]:
                ref = refs[chunk["id"]]
                is_long = "原始政策-0-" in chunk["source_uri"]
                if not is_long or chunk["ordinal"] == 0:
                    value["methods"].append({"task": "退款复核", "boundary": "仅按原始政策复核",
                                             "source_refs": [ref]})
                else:
                    value["facts"].append({"text": "Original supplied example note.", "source_refs": [ref]})
                if chunk["ordinal"] == 0:
                    value.update(context_uncertain=True, context_refs=[ref])
            self.contents[target] = _json(value)
        return accepted

class UnitContextOwner(CompactOwner):
    def __init__(self, root, *, enabled=True):
        super().__init__(root)
        self.ov = self.fixture.ov = UnitContextFake()
        self.enabled = enabled

    def new(self, count=2):
        assert count == 2
        job, entries = super().new(count)
        text = ("Original table heading. Review a supplied request under original conditions. "
                "The mandatory condition has unresolved applicability within this first window.\n"
                + "Ordinary example note, retained verbatim for source audit.\n" * 450)
        Path(entries[0]["local_path"]).write_text(text, encoding="utf-8")
        entries[0].update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return self.repo.update_job(job["job_id"], staged_uncertainty_scope_protocol=(
            UNCERTAINTY_SCOPE_PROTOCOL if self.enabled else ""
        )), entries

def assert_raw_source_complete(root, entries):
    chunks = [_read(path) for path in sorted((root / "chunks").glob("*.json"))]
    for entry in entries:
        original = Path(entry["local_path"]).read_text(encoding="utf-8")
        name = Path(entry["relative_path"]).name
        selected = [chunk for chunk in chunks if Path(chunk["source_uri"]).name == name]
        assert selected and selected[0]["start_char"] == 0
        assert all(left["end_char"] == right["start_char"] for left, right in zip(selected, selected[1:]))
        assert selected[-1]["end_char"] == len(original)
        assert "".join(chunk["content"] for chunk in selected) == original
        assert all(hashlib.sha256(chunk["content"].encode()).hexdigest() == chunk["sha256"] for chunk in selected)
        assert hashlib.sha256(original.encode()).hexdigest() == entry["sha256"]

async def first_reviewed(owner):
    job, entries = owner.new()
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return first, entries
