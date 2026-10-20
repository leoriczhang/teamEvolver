"""Fresh local routing remains lossless and reusable through the full two-round host pipeline."""

from __future__ import annotations

import asyncio

from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from tests.miner.skill_miner.support.direct_workflow_planning import DirectOwner
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class LocalScopeOwner(DirectOwner):
    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"], staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3), entries


def test_local_routing_reaches_complete_dataset_and_reuses_reviewed_round(tmp_path):
    async def check():
        owner = LocalScopeOwner(tmp_path)
        job, entries = owner.new(8)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        root, state = await owner._staged_load(first)
        assert state["source_scope_protocol"] == SOURCE_SCOPE_PROTOCOL_V3
        scope = _read(root / "scopes/refund-review.json")
        assert scope["scope_protocol"] == SOURCE_SCOPE_PROTOCOL_V3
        assert (root / "compilers/extract.md").read_bytes() == (
            root / "aux-compilers/extract_batch.md"
        ).read_bytes()
        assert len(_read(owner.repo.workspace(first) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["source_scope_protocol"] == SOURCE_SCOPE_PROTOCOL_V3
        assert _read(root / "scopes/refund-review.json")["scope_protocol"] == SOURCE_SCOPE_PROTOCOL_V3
        assert state["reused_extract_count"] > 0 and state["reused_candidate_count"] == 1
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before
        assert len(_read(owner.repo.workspace(second) / "compiled_skill/refund-review/benchmark.json")["cases"]) == 12

    asyncio.run(check())


def test_new_scope_setting_cannot_migrate_an_already_frozen_round(tmp_path):
    async def check():
        owner = DirectOwner(tmp_path)
        job = await begin(owner, 8)
        root, before = await owner._staged_load(job)
        frozen = {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}
        job = owner.repo.update_job(job["job_id"], staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3)
        job = await drive(owner, job)
        assert job["status"] == "waiting_human", job.get("error")
        _, after = await owner._staged_load(job)
        assert not before.get("source_scope_protocol") and not after.get("source_scope_protocol")
        assert frozen == {path.name: path.read_bytes() for path in (root / "compilers").glob("*.md")}

    asyncio.run(check())
