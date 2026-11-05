"""Exercise the Worker Host with real PG state and deterministic OV responses."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from team_miner.skill_miner.cache import job_root, persist_cache
from team_miner.skill_miner.engine import SkillMiningEngine
from team_miner.skill_miner.settings import compiler_prompt
from teamEvolver.runtime.repository import ResourceLeaseRepository

pytestmark = pytest.mark.postgres


class Remote:
    def __init__(self):
        self.calls = []
        self.accepted = True
        self.unavailable = False
        self.status = "running"

    async def aclose(self):
        pass

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        data, code, error = {}, 200, ""
        if "/compile/submissions/" in path:
            if self.unavailable:
                code, error = 503, "temporary upstream failure"
            elif self.accepted:
                data = {"task_id": "remote-one"}
            else:
                code, error = 404, "NOT_FOUND"
        elif path == "/api/v1/tasks/remote-one":
            data = {"task_id": "remote-one", "status": self.status, "stage": self.status}
        elif path == "/api/v1/tasks/remote-one/cancel":
            data = {"accepted": True}
        else:
            raise AssertionError((method, path))
        return SimpleNamespace(ok=code == 200, status_code=code, error=error, data=data)


def services(env, tmp_path, remote):
    return SimpleNamespace(tx=env.tx, leases=ResourceLeaseRepository(env.tx),
                           extras={"skill_mining_cache_root": tmp_path,
                                   "skill_mining_client_factory": lambda _miner: remote})


def engine(env, tmp_path, remote):
    return SkillMiningEngine(services(env, tmp_path, remote), "alpha",
                             {"miner": {"account_id": "team", "user_id": "author"}, "settings": {}})


async def create(host, *, name="job-one", **changes):
    await host.prepare()
    value = await asyncio.to_thread(host.repository.create_job,
        job_id=name, name="资料挖掘", account="team", source_uri="viking://resources/source",
        source_snapshot_uri="viking://resources/frozen", output_uri="viking://resources/output",
        document_count=1, max_rounds=3, compiler_body=compiler_prompt({}))
    await asyncio.to_thread(persist_cache, host.repository, value["job_id"])
    if changes:
        value = await asyncio.to_thread(host.repository.update_job, value["job_id"], **changes)
    return value


def test_restarted_worker_recovers_accepted_submission_without_another_post(pg_env, tmp_path):
    async def run():
        remote = Remote()
        first = engine(pg_env, tmp_path, remote)
        job = await create(first, compile_request={"from": ["viking://resources/frozen"],
                                                   "to": "viking://resources/output", "skill": "immutable"})
        frozen = (job_root(first.repository, job["job_id"]) / "compiler-skill.md").read_bytes()
        await first.close()
        restarted = engine(pg_env, tmp_path, remote)
        try:
            queued = await restarted.submit(job["job_id"])
            assert queued["status"] == "queued" and queued["ov_task_id"] == "remote-one"
            assert len(queued["native_compile_history"]) == 1
            assert not any(method == "POST" for method, _path, _body in remote.calls)
            assert (job_root(restarted.repository, job["job_id"]) / "compiler-skill.md").read_bytes() == frozen
            current = await restarted.poll(queued)
            assert current["status"] == "running"
            assert len(current["native_compile_history"]) == 1
        finally:
            await restarted.close()
    asyncio.run(run())


def test_unknown_acceptance_stop_stays_stopping_until_remote_lookup_proves_absence(pg_env, tmp_path):
    async def run():
        remote, host = Remote(), None
        host = engine(pg_env, tmp_path, remote)
        try:
            job = await create(host, compile_request={"from": [], "to": "target", "skill": "immutable"})
            await host.admission.acquire(job, {"key": job["submission_idempotency_key"]})
            stopped = await host.stop(job, "人工停止")
            assert stopped["status"] == "stopping" and host.pending_submission
            assert remote.calls == []
            remote.unavailable = True
            unresolved = await host.submit(job)
            assert unresolved["status"] == "stopping" and not unresolved["ov_task_id"]
            remote.unavailable, remote.accepted = False, False
            confirmed = await host.submit(job)
            assert confirmed["status"] == "stopped"
            assert await host.admission.reservations() == []
            assert not any(method == "POST" for method, _path, _body in remote.calls)
        finally:
            await host.close()
    asyncio.run(run())


def test_same_engine_reloads_artifacts_after_another_owner_edit(pg_env, tmp_path):
    async def run():
        remote = Remote()
        old, editor = engine(pg_env, tmp_path, remote), engine(pg_env, tmp_path, remote)
        try:
            job = await create(old)
            await old.restore(job["job_id"])
            await editor.prepare()
            async with editor.job_lock(job["job_id"]):
                await editor.restore(job["job_id"])
                path = job_root(editor.repository, job["job_id"]) / "human-evidence.md"
                path.write_bytes(b"accepted edit on another replica")
                await asyncio.to_thread(editor.repository.update_job, job["job_id"], edit_receipt="editor")
            await old.restore(job["job_id"])
            assert (job_root(old.repository, job["job_id"]) / "human-evidence.md").read_bytes() == (
                b"accepted edit on another replica")
        finally:
            await old.close()
            await editor.close()
    asyncio.run(run())


def test_resource_lease_loss_blocks_provisioning_and_pg_mutation(pg_env, tmp_path):
    async def run():
        remote, host = Remote(), None
        host = engine(pg_env, tmp_path, remote)
        try:
            job = await create(host)
            with pytest.raises(RuntimeError, match="lease was lost"):
                async with host.job_lock(job["job_id"]):
                    await host.restore(job["job_id"])
                    async with pg_env.tx.tenant_tx("alpha") as conn:
                        await conn.execute(
                            f"UPDATE {pg_env.schema}.resource_leases SET owner_id='replacement',fence=fence+1 "
                            "WHERE tenant_id=$1 AND resource_key=$2", "alpha", "skill-mining:job:" + job["job_id"])
                    with pytest.raises(RuntimeError, match="lease was lost"):
                        await host._round_client(job).get_skill(skill_name="immutable")
                    with pytest.raises(RuntimeError, match="lease was lost"):
                        await asyncio.to_thread(host.repository.update_job, job["job_id"], status="succeeded")
            assert remote.calls == []
            assert host.repository.get_job(job["job_id"])["status"] == "preparing"
        finally:
            await host.close()
    asyncio.run(run())


def test_default_v8_multi_round_worker_restores_frozen_compiler_and_source_bytes(pg_env, tmp_path):
    from tests.miner.skill_miner.support.platform import SOURCE, PlatformRemote

    async def run():
        remote = PlatformRemote()
        settings = {"miner": {"account_id": "default", "user_id": "author"}, "settings": {}}
        first = SkillMiningEngine(services(pg_env, tmp_path, remote), "alpha", settings)
        await first.prepare()
        try:
            job = await asyncio.to_thread(first.repository.create_job,
                job_id="v8-rounds", name="完整课程资料", account="default", source_uri=SOURCE,
                source_snapshot_uri="viking://resources/frozen", output_uri="viking://resources/output",
                document_count=1, max_rounds=2, compiler_body=compiler_prompt({}))
            await asyncio.to_thread(persist_cache, first.repository, job["job_id"])
            submitted = await first.submit(job)
            assert submitted["status"] == "queued"
            assert len(remote.remote.compile_calls) == 1
            root = job_root(first.repository, job["job_id"])
            frozen_compiler = (root / "compiler-skill.md").read_bytes()
            frozen_source = (root / "round_inputs/round-1/manifest.json").read_bytes()
            remote.complete(submitted)
            done = await first.poll(submitted)
            assert done["status"] == "succeeded" and not done["content_validated"]
            assert not list(first.repository.workspace(done).rglob("benchmark.json"))
            async with first.job_lock(job["job_id"]):
                await first.restore(job["job_id"])
                checkpoint = first.repository.public_job(done)["optional_revision"]
                second = await asyncio.to_thread(first.repository.prepare_next_round,
                    job["job_id"], checkpoint["id"], {}, [], {}, "按新增要求完善课程", "revision-two")
            await first.close()
            restarted = SkillMiningEngine(services(pg_env, tmp_path, remote), "alpha", settings)
            try:
                second = await restarted.submit(second)
                assert len(remote.remote.compile_calls) == 2 and second["current_round"] == 2
                assert second["output_uri"] == submitted["output_uri"]
                assert "按新增要求完善课程" in second["compile_request"]["instruction"]
                new_root = job_root(restarted.repository, job["job_id"])
                assert (new_root / "compiler-skill.md").read_bytes() == frozen_compiler
                # Local locator paths change, while the frozen content and its hashes remain bound.
                import json
                old_manifest, new_manifest = json.loads(frozen_source), json.loads(
                    (new_root / "round_inputs/round-1/manifest.json").read_bytes())
                assert [(r["relative_path"], r["sha256"]) for r in old_manifest] == [
                    (r["relative_path"], r["sha256"]) for r in new_manifest]
                remote.complete(second, "第二轮修订正文")
                final = await restarted.poll(second)
                assert final["status"] == "succeeded" and len(final["rounds_history"]) == 2
                assert not final["content_validated"] and not final["artifact_quality"]["can_submit"]
                output = restarted.repository.workspace(final) / "compiled_skill/lesson/SKILL.md"
                assert output.read_text() == "第二轮修订正文"
            finally:
                await restarted.close()
        finally:
            await first.close()
    asyncio.run(run())
