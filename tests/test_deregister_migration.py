"""Migration checks verify persistence boundaries, not the command parser."""
import copy
from types import SimpleNamespace

import pytest

from scripts import migrate_deregister as migration
from teamEvolver.config import TeamEvolverConfig


def evidence():
    return {
        "writers_stopped": True, "clients_use_user_id": True,
        "full_release_cycle_observed": True, "strict_business_cycle_observed": True,
        "legacy_requests": 0, "observation_start": "2026-01-01T00:00:00Z",
        "observation_end": "2026-02-01T00:00:00Z",
        "skill_pull_isolation_verified": ["default", "a", "b"],
        "replay_binding_verified_or_unused": ["default", "a", "b"],
    }


def state(user="alice"):
    return {"sessions": {"existing-session-id": {"user_id": user, "agent_id": "old"}},
            "refs": {}, "snapshots": {"snap": {"user_id": user, "agent_id": "old",
            "subject": {"user_id": user, "integration_id": "old"}, "items": []}}}


@pytest.fixture
def backend(monkeypatch, tmp_path):
    config = TeamEvolverConfig(storage_pg_enabled=True, storage_pg_dsn="test-only",
                              users_registry_path=str(tmp_path / "users.json"),
                              agent_protocol_identity_mode="tenant_user", skills_delivery_mode="pull")
    db = {(tid, migration.CONTEXT): state() for tid in ["default", "a", "b"]}
    db.update({(tid, migration.AGENTS): {"agents": [{"agent_id": "old"}]} for tid in ["default", "a", "b"]})
    db["default", "users.json"] = {"users": [{"id": "alice", "agent_subjects": [{"x": "y"}], "agent_identities": {"hermes": "alice"}}]}
    writes = []
    def read_kv(config, name, path, *, tenant_id):
        assert tenant_id
        return copy.deepcopy(db.get((tenant_id, name), {}))
    def write_kv(config, name, path, value, *, tenant_id):
        assert tenant_id
        writes.append((tenant_id, name))
        db[tenant_id, name] = copy.deepcopy(value)
    monkeypatch.setattr(migration, "read_kv", read_kv)
    monkeypatch.setattr(migration, "write_kv", write_kv)
    registry = SimpleNamespace(list_tenants=lambda: [SimpleNamespace(tenant_id=tid) for tid in ["a", "b"]])
    return config, db, writes, registry


def test_dry_run_is_read_only_and_all_tenants_are_explicit(backend):
    cfg, db, writes, reg = backend
    before = copy.deepcopy(db)
    report = migration.migrate(cfg, registry=reg)
    assert writes == [] and db == before
    assert report["tenants"] == ["a", "b", "default"]
    assert report["global_users_cleaned"]


def test_apply_repeat_restore_checksums_and_session_ids(backend):
    cfg, db, writes, reg = backend
    before = copy.deepcopy(db)
    report = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    for tid in report["tenants"]:
        context = db[tid, migration.CONTEXT]
        assert list(context["sessions"]) == ["existing-session-id"]
        assert context["sessions"]["existing-session-id"]["tenant_id"] == tid
        assert context["sessions"]["existing-session-id"]["agent_id_legacy"] == "old"
        assert context["snapshots"]["snap"]["subject"] == {"tenant_id": tid, "user_id": "alice"}
    assert "agent_subjects" not in db["default", "users.json"]["users"][0]
    second = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    assert second == report
    migration.restore(cfg, report, writers_stopped=True)
    for key, value in before.items():
        assert db[key] == value
    migration.restore(cfg, report, writers_stopped=True)
    assert all("before_count" in entry and "after_sha256" in entry for entry in report["objects"])


def test_single_tenant_does_not_delete_global_mappings(backend):
    cfg, db, writes, reg = backend
    migration.migrate(cfg, tenants=["a"], registry=reg, apply=True, evidence=evidence())
    assert "agent_subjects" in db["default", "users.json"]["users"][0]
    assert "tenant_id" not in db["b", migration.CONTEXT]["sessions"]["existing-session-id"]


@pytest.mark.parametrize("bad", [None, "bob/unsafe", " alice "])
def test_unknown_user_is_not_inferred(backend, bad):
    cfg, db, writes, reg = backend
    db["b", migration.CONTEXT] = state(bad)
    with pytest.raises(ValueError):
        migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    assert writes == []


def test_wrong_tenant_fails_before_backup(backend):
    cfg, db, writes, reg = backend
    db["b", migration.CONTEXT]["sessions"]["existing-session-id"]["tenant_id"] = "a"
    with pytest.raises(ValueError, match="tenant conflict"):
        migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    assert writes == []


def test_context_write_failure_keeps_users_and_original_backups(backend, monkeypatch):
    cfg, db, writes, reg = backend
    original = copy.deepcopy(db)
    real_write = migration.write_kv
    def fail(config, name, path, data, *, tenant_id):
        if tenant_id == "b" and name == migration.CONTEXT:
            raise RuntimeError("injected disk failure")
        real_write(config, name, path, data, tenant_id=tenant_id)
    monkeypatch.setattr(migration, "write_kv", fail)
    with pytest.raises(RuntimeError):
        migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    assert db["default", "users.json"] == original["default", "users.json"]
    for tid in ["a", "b", "default"]:
        assert db[tid, "agent_context_state.pre-deregister.json"] == original[tid, migration.CONTEXT]
    monkeypatch.setattr(migration, "write_kv", real_write)
    report = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    migration.restore(cfg, report, writers_stopped=True)
    assert all(db[key] == value for key, value in original.items())


def test_corrupt_backup_prevents_any_restore_write(backend):
    cfg, db, writes, reg = backend
    report = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    db["b", "agent_context_state.pre-deregister.json"] = {"corrupt": True}
    writes.clear()
    with pytest.raises(ValueError, match="checksum"):
        migration.restore(cfg, report, writers_stopped=True)
    assert writes == []


def test_observation_and_writes_must_be_attested(backend):
    cfg, db, writes, reg = backend
    with pytest.raises(ValueError, match="evidence"):
        migration.migrate(cfg, registry=reg, apply=True)
    assert writes == []


def test_file_backend_and_scope_guard(tmp_path):
    cfg = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"),
                           agent_protocol_identity_mode="tenant_user", skills_delivery_mode="pull")
    migration.write(cfg, migration.USERS, "default", {"users": [{"id": "alice", "agent_subjects": []}]})
    migration.write(cfg, migration.CONTEXT, "default", state())
    report = migration.migrate(cfg, apply=True, evidence=evidence())
    assert (tmp_path / "agent_context_state.pre-deregister.json").exists()
    assert (tmp_path / "users.pre-deregister.json").stat().st_mode & 0o777 == 0o600
    wrong = copy.deepcopy(report)
    wrong["scope"]["context_path"] = "/wrong"
    with pytest.raises(ValueError, match="scope"):
        migration.restore(cfg, wrong, writers_stopped=True)
    migration.restore(cfg, report, writers_stopped=True)
    assert migration.read(cfg, migration.CONTEXT, "default") == state()


def test_partial_then_all_tenants_reuses_original_backups(backend):
    cfg, db, writes, reg = backend
    before = copy.deepcopy(db)
    migration.migrate(cfg, tenants=["a"], registry=reg, apply=True, evidence=evidence())
    report = migration.migrate(cfg, registry=reg, apply=True, evidence=evidence())
    migration.restore(cfg, report, writers_stopped=True)
    assert all(db[key] == value for key, value in before.items())
