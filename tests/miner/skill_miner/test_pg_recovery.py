"""Frozen recovery receipts and legacy reuse survive a change of Worker cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from team_miner.skill_miner.cache import job_root, persist_cache, restore_cache
from team_miner.skill_miner.core_repository import OvSkillMiningRepository
from team_miner.skill_miner.native_recovery import pending_guidance, prepare_explicit_retry, prepare_recovery
from team_miner.skill_miner.repository import PgMiningBackend, make_repository
from tests.miner.skill_miner.support.native_explicit_retry import authorization
from tests.miner.skill_miner.support.native_mining_rounds import NativeOwner
from tests.miner.skill_miner.support.native_resource_recovery import failed_second_round

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("explicit", [False, True], ids=["contract-recovery", "explicit-retry"])
def test_native_receipt_preserved_hashes_survive_pg_and_new_cache(pg_env, tmp_path, explicit):
    async def run():
        _owner, original, remote, failed, task = await failed_second_round(
            tmp_path / "original", protocol_failure=explicit
        )
        root = original.jobs_root / failed["job_id"]
        auth = authorization(failed) if explicit else None
        receipt = (
            prepare_explicit_retry(root, failed, authorization=auth, apply=True, remote_task=task)
            if explicit
            else prepare_recovery(root, failed, apply=True, remote_task=task)
        )
        original_files = {name: (root / name).read_bytes() for name in receipt["preserved_sha256"]}
        backend = PgMiningBackend(pg_env.tx, "alpha")
        backend.cache_project_root = original.project_root
        backend.save_job(failed)
        persisted = OvSkillMiningRepository(original.state_root, original.project_root, backend=backend)
        persist_cache(persisted, failed["job_id"])

        services = SimpleNamespace(tx=pg_env.tx, extras={"skill_mining_cache_root": tmp_path / "new-owner"})
        restarted = make_repository(services, "alpha")
        restarted.get_job(failed["job_id"])
        restore_cache(restarted, failed["job_id"])
        restored_root = job_root(restarted, failed["job_id"])
        assert restored_root != root
        for name, digest in receipt["preserved_sha256"].items():
            raw = (restored_root / name).read_bytes()
            assert raw == original_files[name] and hashlib.sha256(raw).hexdigest() == digest
        recovered = restarted.resume_native_failure(failed["job_id"], task, authorization=auth)
        guidance = pending_guidance(restored_root, recovered, "")
        assert guidance == receipt["guidance"]
        host = NativeOwner(restarted, remote)
        submitted = await host._submit_ov_skill_round(recovered)
        assert submitted["status"] == "queued" and len(remote.compile_calls) == 3
        assert submitted["compiler_uri"] == failed["compiler_uri"]
        assert submitted["compile_request"]["from"] == failed["compile_request"]["from"]
        for name, digest in receipt["preserved_sha256"].items():
            assert hashlib.sha256((restored_root / name).read_bytes()).hexdigest() == digest
        manifest = json.loads((restored_root / "round_inputs/round-1/manifest.json").read_bytes())
        victim = restored_root / "round_inputs/round-1" / __import__("pathlib").Path(manifest[0]["local_path"]).name
        victim.write_bytes(victim.read_bytes() + b"changed")
        with pytest.raises(ValueError, match="冻结.*发生变化"):
            pending_guidance(restored_root, recovered, "")

    asyncio.run(run())


def test_legacy_second_round_reuse_journal_continues_after_pg_cache_rebuild(pg_env, tmp_path):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from team_miner.skill_miner.round_reuse import _load
    from tests.miner.skill_miner.support.ov_staged_orchestration import drive
    from tests.miner.skill_miner.support.round_reuse import POLICY, ReuseOwner, next_inputs, reviewed_first

    async def run():
        original = ReuseOwner(tmp_path / "original")
        first = await reviewed_first(original)
        second, inputs = await next_inputs(original, first)
        second = await original._prepare_staged_round(second, SKILL_BODY, inputs, POLICY, "")
        old_root, _old_state = await original._staged_load(second)
        frozen = {name: (old_root / "reuse" / name).read_bytes() for name in ("manifest.json", "sources.json")}
        metadata = PgMiningBackend(pg_env.tx, "alpha")
        metadata.cache_project_root = original.repo.project_root
        metadata.save_job(second)
        persisted = OvSkillMiningRepository(original.repo.state_root, original.repo.project_root, backend=metadata)
        persist_cache(persisted, second["job_id"])
        services = SimpleNamespace(tx=pg_env.tx, extras={"skill_mining_cache_root": tmp_path / "new-owner"})
        repository = make_repository(services, "alpha")
        job = repository.get_job(second["job_id"])
        restore_cache(repository, job["job_id"])
        restarted = ReuseOwner(tmp_path / "fixture")
        restarted.repo = restarted.fixture.repo = repository
        restarted.ov = restarted.fixture.ov = original.ov
        root, state = await restarted._staged_load(job)
        header, sources = _load(job, root, state)
        assert header["enabled"] and str(repository.project_root) in header["previous_root"]
        assert all(str(repository.project_root) in row["local_path"] for row in sources["current"].values())
        completed = await drive(restarted, job)
        assert completed["status"] == "waiting_human" and completed["current_round"] == 2
        assert completed["candidate_count"] == first["candidate_count"]
        _, final_state = await restarted._staged_load(completed)
        assert final_state["reused_candidate_count"] > 0
        for name, raw in frozen.items():
            assert (root / "reuse" / name).read_bytes() == raw
        from pathlib import Path

        source = Path(next(iter(sources["current"].values()))["local_path"])
        source.write_bytes(source.read_bytes() + b"changed")
        with pytest.raises(ValueError, match="SHA changed"):
            _load(job, root, state)

    asyncio.run(run())


def test_checkpoint_replay_with_attachment_keeps_original_fingerprint_across_pg_cache(pg_env, tmp_path):
    import base64

    from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

    async def run():
        host, repository, remote, job = new_owner(tmp_path / "original")
        first = await host._submit_ov_skill_round(job)
        complete(remote, first, question=True)
        await host._refresh_ov_skill_job(first)
        waiting = repository.get_job(job["job_id"])
        checkpoint = waiting["pending_checkpoint"]["id"]
        waiting = await host._stage_round_supplements(waiting, checkpoint, [{
            "name": "evidence.md", "content_base64": base64.b64encode("真实补证".encode()).decode()}])
        arguments = (job["job_id"], checkpoint, {"region": "仅甲地区"}, [], {}, "", "same-operation")
        prepared = repository.prepare_next_round(*arguments, submitted_by="reviewer")
        original_fingerprint = prepared["last_checkpoint_submission"]["fingerprint"]
        metadata = PgMiningBackend(pg_env.tx, "alpha")
        metadata.cache_project_root = repository.project_root
        metadata.save_job(prepared)
        persist_cache(OvSkillMiningRepository(repository.state_root, repository.project_root, backend=metadata),
                      prepared["job_id"])
        services = SimpleNamespace(tx=pg_env.tx, extras={"skill_mining_cache_root": tmp_path / "new-owner"})
        restarted = make_repository(services, "alpha")
        restarted.get_job(prepared["job_id"])
        restore_cache(restarted, prepared["job_id"])
        repeated = restarted.prepare_next_round(*arguments, submitted_by="reviewer")
        assert repeated["current_round"] == 2
        assert repeated["last_checkpoint_submission"]["fingerprint"] == original_fingerprint
        with pytest.raises(ValueError, match="重复请求内容不一致"):
            restarted.prepare_next_round(*arguments[:5], "不同内容", arguments[6], submitted_by="reviewer")
    asyncio.run(run())
