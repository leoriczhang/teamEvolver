"""Phase-6 deletion is scoped, backed up, repeatable and recoverable."""
import copy

import pytest

from scripts import finalize_deregister as cleanup
from scripts import migrate_deregister as migration
from test_deregister_migration import backend, evidence  # noqa: F401


@pytest.fixture
def ready(backend, monkeypatch):
    cfg, db, writes, reg = backend
    manifest = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    deletions = []

    def delete(config, tenant):
        deletions.append(tenant)
        db.pop((tenant, migration.AGENTS), None)

    monkeypatch.setattr(cleanup, "delete_agents", delete)
    writes.clear()
    return cfg, db, writes, reg, manifest, deletions


def attestation():
    return {**evidence(), "phase5_stable_cycle_observed": True}


def test_finalize_dry_run_apply_repeat_and_restore(ready):
    cfg, db, writes, reg, phase5, deletions = ready
    before = copy.deepcopy(db)
    report = cleanup.finalize(cfg, phase5, registry=reg)
    assert report["action"] == "dry-run" and not writes and not deletions and db == before
    report = cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation())
    for tid in report["tenants"]:
        assert (tid, migration.AGENTS) not in db
        session = db[tid, migration.CONTEXT]["sessions"]["existing-session-id"]
        assert "agent_id_legacy" not in session and session["user_id"] == "alice"
        assert db[tid, "agents.pre-deregister.json"] == before[tid, "agents.pre-deregister.json"]
    assert cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation()) == report
    cleanup.restore(cfg, report, writers_stopped=True)
    assert all(db[k] == v for k, v in before.items())
    # A version rollback can then restore phase 5 too, when no new writes exist.
    migration.restore(cfg, phase5, writers_stopped=True)
    assert "agent_subjects" in db["default", migration.USERS]["users"][0]


def test_finalize_requires_separate_observation(ready):
    cfg, db, writes, reg, phase5, deletions = ready
    with pytest.raises(ValueError, match="phase5_stable"):
        cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=evidence())
    assert not writes and not deletions


@pytest.mark.parametrize("breakage", ["backup", "tenant", "unapplied", "mapping", "new_agent"])
def test_preflight_failure_never_deletes_any_registry(ready, breakage):
    cfg, db, writes, reg, phase5, deletions = ready
    if breakage == "backup":
        db["b", "agents.pre-deregister.json"] = {"bad": True}
    elif breakage == "tenant":
        db["b", migration.CONTEXT]["sessions"]["existing-session-id"]["tenant_id"] = "a"
    elif breakage == "unapplied":
        db.pop(("b", phase5["manifest_key"]))
    elif breakage == "new_agent":
        db["b", migration.AGENTS]["agents"].append({"agent_id": "late-registration"})
    else:
        db["default", migration.USERS]["users"][0]["agent_subjects"] = []
    with pytest.raises(ValueError):
        cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation())
    assert not writes and not deletions


def test_failed_context_write_preserves_registries_and_resumes(ready, monkeypatch):
    cfg, db, writes, reg, phase5, deletions = ready
    original = migration.write

    def fail(config, key, tid, value):
        if key == migration.CONTEXT and tid == "b":
            raise RuntimeError("injected storage failure")
        original(config, key, tid, value)

    monkeypatch.setattr(migration, "write", fail)
    with pytest.raises(RuntimeError):
        cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation())
    assert not deletions
    monkeypatch.setattr(migration, "write", original)
    assert cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation())["action"] == "applied"


def test_restore_rejects_newer_writes_before_touching_any_scope(ready):
    cfg, db, writes, reg, phase5, deletions = ready
    report = cleanup.finalize(cfg, phase5, registry=reg, apply=True, evidence=attestation())
    db["b", migration.CONTEXT]["sessions"]["new"] = {"tenant_id": "b", "user_id": "alice"}
    writes.clear()
    with pytest.raises(ValueError, match="newer source"):
        cleanup.restore(cfg, report, writers_stopped=True)
    assert not writes


def test_file_registry_is_physically_removed_and_restored(tmp_path):
    from teamEvolver.config import TeamEvolverConfig
    from test_deregister_migration import state

    cfg = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"))
    # Compatibility releases expose these temporary switches; final releases do not.
    if hasattr(cfg, "agent_protocol_identity_mode"):
        cfg.agent_protocol_identity_mode = "tenant_user"
        cfg.skills_delivery_mode = "pull"
    migration.write(cfg, migration.CONTEXT, "default", state())
    migration.write(cfg, migration.USERS, "default", {"users": [{"id": "alice", "agent_subjects": []}]})
    migration.write(cfg, migration.AGENTS, "default", {"agents": [{"agent_id": "old"}]})
    phase5 = migration.migrate(cfg, apply=True, evidence=evidence())
    report = cleanup.finalize(cfg, phase5, apply=True, evidence=attestation())
    assert not (tmp_path / "agents.json").exists()
    cleanup.restore(cfg, report, writers_stopped=True)
    assert (tmp_path / "agents.json").exists()
