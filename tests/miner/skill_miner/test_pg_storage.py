"""Real PG restart, tenant-isolation and concurrent-write guarantees for mining."""

from __future__ import annotations

import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from team_miner.skill_miner.cache import cache_root, persist_cache, restore_cache
from team_miner.skill_miner.config import freeze, load, save
from team_miner.skill_miner.repository import MiningCapacityExceeded, MiningRevisionConflict, PgMiningBackend
from team_miner.skill_miner.storage import PgMiningFileStore
from teamEvolver.config_runtime import ConfigRepository, TenantConfigMissing
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres


def backend(env, tenant="alpha", **kwargs):
    return PgMiningBackend(env.tx, tenant, **kwargs)


def job(name="job-1", **changes):
    return {"job_id": name, "status": "queued", "workspace": f"mining_jobs/{name}/workspace", **changes}


def source(name):
    return {"source_id": name, "account": "team", "uri": f"viking://resources/sources/{name}",
            "name": name, "tags": ["skill"], "default": False}


def test_metadata_survives_new_backend_and_tenants_have_independent_identical_ids(pg_env):
    alpha, beta = backend(pg_env), backend(pg_env, "beta")
    alpha.save_job(job(detail="alpha-only"))
    beta.save_job(job(detail="beta-only"))
    alpha.load_sources()
    alpha.save_sources([source("source-1")])

    restarted = backend(pg_env)
    assert restarted.load_job("job-1")["detail"] == "alpha-only"
    assert backend(pg_env, "beta").load_job("job-1")["detail"] == "beta-only"
    assert len(restarted.list_jobs()) == 1
    assert restarted.load_sources() == [source("source-1")]
    assert beta.load_sources() == []


def test_job_compare_and_swap_rejects_stale_replica_including_tombstones(pg_env):
    first, second = backend(pg_env), backend(pg_env)
    first.save_job(job())
    before = second.load_job("job-1")
    first.load_job("job-1")
    first.save_job(job(status="running"))

    with pytest.raises(MiningRevisionConflict):
        second.save_job({**before, "status": "stopped"})
    assert second.load_job("job-1")["status"] == "running"
    second.save_job({**second.load_job("job-1"), "_deleted": True})
    assert backend(pg_env).load_job("job-1")["_deleted"] is True
    with pytest.raises(MiningRevisionConflict):
        backend(pg_env).save_job(job())


def test_source_snapshot_preserves_concurrent_additions_and_rejects_lost_edits(pg_env):
    first, second = backend(pg_env), backend(pg_env)
    first.load_sources()
    second.load_sources()
    second.save_sources([source("source-b")])
    first.save_sources([source("source-a")])
    assert {s["source_id"] for s in first.load_sources()} == {"source-a", "source-b"}
    old = second.load_sources()
    current = first.load_sources()
    current[0]["name"] = "reviewed"
    first.save_sources(current)
    old[0]["name"] = "stale edit"
    with pytest.raises(MiningRevisionConflict):
        second.save_sources(old)
    assert first.load_sources()[0]["name"] == "reviewed"


def test_pg_artifacts_rebuild_deleted_cache_and_remove_stale_local_files(pg_env, tmp_path):
    first = backend(pg_env)
    first.save_job(job())
    root = tmp_path / "first"
    local = root / "mining_jobs/job-1/round_inputs/round-1"
    local.mkdir(parents=True)
    content = b"frozen source and compiler evidence\x00"
    (local / "source.bin").write_bytes(content)
    repo = SimpleNamespace(project_root=root, backend=first)
    persist_cache(repo, "job-1")
    shutil.rmtree(root)

    other_root = tmp_path / "other-pod"
    other = SimpleNamespace(project_root=other_root, backend=backend(pg_env))
    stale = other_root / "mining_jobs/job-1/stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("uncommitted local file")
    result = restore_cache(other, "job-1")
    assert result["files"] == 1
    assert not stale.exists()
    assert (other_root / "mining_jobs/job-1/round_inputs/round-1/source.bin").read_bytes() == content
    with pytest.raises(KeyError):
        PgMiningFileStore(backend(pg_env, "beta")).load("job-1")


def test_artifact_snapshot_cas_is_independent_of_metadata_reload(pg_env):
    first, second = backend(pg_env), backend(pg_env)
    first.save_job(job())
    second.load_job("job-1")
    PgMiningFileStore(first).save("job-1", {"workspace/SKILL.md": b"committed"})
    second.load_job("job-1")  # Metadata reload cannot bless a stale artifact cache.
    with pytest.raises(MiningRevisionConflict):
        PgMiningFileStore(second).save("job-1", {"workspace/SKILL.md": b"stale"})
    assert PgMiningFileStore(second).load("job-1") == {"workspace/SKILL.md": b"committed"}
    PgMiningFileStore(second).save("job-1", {})
    assert PgMiningFileStore(first).load("job-1") == {}


def test_artifacts_reject_traversal_and_symlinks_before_pg_changes(pg_env, tmp_path):
    store = backend(pg_env)
    store.save_job(job())
    with pytest.raises(ValueError, match="unsafe"):
        PgMiningFileStore(store).save("job-1", {"../outside": b"data"})
    local = tmp_path / "mining_jobs/job-1"
    local.mkdir(parents=True)
    (local / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symbolic"):
        persist_cache(SimpleNamespace(project_root=tmp_path, backend=store), "job-1")
    assert PgMiningFileStore(store).load("job-1") == {}


def test_worker_write_is_fenced_before_metadata_or_artifact_mutation(pg_env):
    normal = backend(pg_env)
    normal.save_job(job())

    async def stale(_conn, _task, *, allow_cancel=False):
        raise RuntimeError("Worker lease lost")

    services = SimpleNamespace(tasks=SimpleNamespace(require_current=stale))
    old_worker = backend(pg_env, services=services, task=object())
    old_worker.load_job("job-1")
    with pytest.raises(RuntimeError, match="lease lost"):
        old_worker.save_job(job(status="running"))
    with pytest.raises(RuntimeError, match="lease lost"):
        PgMiningFileStore(old_worker).save("job-1", {"source.md": b"stale"})
    assert normal.load_job("job-1")["status"] == "queued"
    assert PgMiningFileStore(normal).load("job-1") == {}


def test_settings_require_miner_config_and_freeze_independent_revisions(pg_env):
    services = SimpleNamespace(tx=pg_env.tx, extras={"config_repo": ConfigRepository(pg_env.tx)})
    initial = _run(pg_env, "alpha", lambda conn: load(services, conn))
    assert initial.revision == 0 and initial.miner_revision == 1
    saved = _run(pg_env, "alpha", lambda conn: save(services, conn, {"mining": {}}, expected_revision=0))
    assert saved.revision == 1
    assert freeze(saved)["settings_revision"] == 1
    assert freeze(saved)["miner_revision"] == 1
    with pytest.raises(MiningRevisionConflict):
        _run(pg_env, "alpha", lambda conn: save(services, conn, {}, expected_revision=0))
    assert _run(pg_env, "beta", lambda conn: load(services, conn)).revision == 0

    async def remove(conn):
        await conn.execute(f"DELETE FROM {pg_env.schema}.tenant_configs WHERE module='miner'")

    _run(pg_env, "alpha", remove)
    with pytest.raises(TenantConfigMissing):
        _run(pg_env, "alpha", lambda conn: load(services, conn))


def test_native_frozen_inputs_and_host_locators_move_across_isolated_caches(pg_env, tmp_path):
    from team_miner.skill_miner.ov_round_orchestration_helpers import _cache_inputs, _cached_inputs

    services = SimpleNamespace(tx=pg_env.tx, extras={"skill_mining_cache_root": tmp_path})
    old, new = cache_root(services, "alpha"), cache_root(services, "alpha")
    assert old != new
    first = backend(pg_env)
    first.cache_project_root = old
    original = old / "mining_jobs/job-1"
    original.mkdir(parents=True)
    (original / "compiler-skill.md").write_text("unchanged compiler")
    _cache_inputs(original / "round_inputs/round-1", [{"kind": "source", "relative_path": "lesson.md",
                  "origin_uri": "viking://resources/source/lesson.md", "content": "真实原文"}])
    first.save_job(job(compiler_path=str(original / "compiler-skill.md"),
                       input_manifest=str(original / "round_inputs/round-1/manifest.json")))
    persist_cache(SimpleNamespace(project_root=old, backend=first), "job-1")
    shutil.rmtree(old)

    restarted = backend(pg_env)
    restarted.cache_project_root = new
    value = restarted.load_job("job-1")
    restore_cache(SimpleNamespace(project_root=new, backend=restarted), "job-1")
    assert Path(value["compiler_path"]).read_text() == "unchanged compiler"
    inputs = _cached_inputs(Path(value["input_manifest"]).parent)
    assert len(inputs) == 1
    assert Path(inputs[0]["local_path"]).read_text() == "真实原文"
    assert str(new) in inputs[0]["local_path"]


def test_before_save_failure_does_not_commit_terminal_metadata(pg_env):
    store = backend(pg_env)
    store.save_job(job())

    def checkpoint_failed(_job):
        raise RuntimeError("artifact checkpoint failed")

    store.before_save = checkpoint_failed
    with pytest.raises(RuntimeError, match="checkpoint failed"):
        store.save_job(job(status="completed"))
    assert backend(pg_env).load_job("job-1")["status"] == "queued"


def test_pg_active_job_cap_serializes_replicas_and_also_checks_resume(pg_env):
    def create(name):
        store = backend(pg_env, max_active=1)
        try:
            store.save_job(job(name))
            return name
        except MiningCapacityExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        names = list(workers.map(create, ["job-a", "job-b"]))
    assert sum(name is not None for name in names) == 1
    store = backend(pg_env, max_active=1)
    active = next(name for name in names if name)
    store.load_job(active)
    store.save_job(job(active, status="running"))  # Its existing slot remains valid.
    store.save_job(job("waiting", status="waiting_review"))
    with pytest.raises(MiningCapacityExceeded):
        store.save_job(job("waiting", status="preparing"))
    store.save_job(job(active, status="completed"))
    store.save_job(job("waiting", status="preparing"))


def test_stream_checkpoint_reads_only_changed_files_and_rolls_back_partial_updates(pg_env):
    store = backend(pg_env)
    store.save_job(job())
    files = PgMiningFileStore(store)
    files.save("job-1", {"unchanged.txt": b"fixed", "changed.txt": b"old", "removed.txt": b"delete"})

    def forbidden_read():
        raise AssertionError("unchanged file must not be read or retransferred")

    files.checkpoint("job-1", [
        ("unchanged.txt", 5, hashlib.sha256(b"fixed").hexdigest(), forbidden_read),
        ("changed.txt", 3, hashlib.sha256(b"new").hexdigest(), lambda: b"new"),
    ])
    assert files.load("job-1") == {"unchanged.txt": b"fixed", "changed.txt": b"new"}

    def broken_stream():
        yield "changed.txt", 4, hashlib.sha256(b"oops").hexdigest(), lambda: b"oops"
        raise RuntimeError("cache read interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        files.checkpoint("job-1", broken_stream())
    assert PgMiningFileStore(backend(pg_env)).load("job-1")["changed.txt"] == b"new"


def test_archived_file_can_exceed_the_source_per_file_limit_without_a_new_storage_limit(pg_env):
    store = backend(pg_env)
    store.save_job(job())
    data = b"log\n" * (21 * 1024 * 1024 // 4)
    files = PgMiningFileStore(store)
    files.checkpoint("job-1", [("run.log", len(data), hashlib.sha256(data).hexdigest(), lambda: data)])
    restored = []
    files.materialize("job-1", lambda path, content: restored.append((path, len(content))))
    assert restored == [("run.log", len(data))]
