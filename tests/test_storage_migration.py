from __future__ import annotations

import json

from teamEvolver.cli.storage_cmd import (
    collect_control_plane_objects,
    migrate_local_control_plane,
)
from teamEvolver.config_store import ConfigStore
from teamEvolver.storage import InMemoryObjectStore


class TransactionalMemoryStore(InMemoryObjectStore):
    native_batch_write = True

    def batch_write(self, objects, *, preconditions=None, **_kwargs):
        for key, condition in (preconditions or {}).items():
            exists = key in self._data
            if condition.get("kind") == "create_if_absent" and exists:
                raise RuntimeError(f"conflict: {key}")
        for key, value in objects.items():
            self.put_object(key, value)
        return {"succeeded": sorted(objects), "failed": [], "mode": "transactional"}


def test_pg_defaults_skill_and_session_backends(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEAMEVOLVER_SKILL_STORAGE_BACKEND", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("storage_pg:\n  enabled: true\n  dsn: postgresql://test\n")

    config = ConfigStore(config_file).to_config()

    assert config.sharing_skill_backend == "postgres"
    assert config.sharing_session_backend == "postgres"


def test_explicit_skill_backend_is_preserved(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "storage_pg:\n  enabled: true\n  dsn: postgresql://test\n"
        "sharing:\n  skill_backend: viking\n"
    )

    config = ConfigStore(config_file).to_config()

    assert config.sharing_skill_backend == "viking"
    assert config.sharing_session_backend == "postgres"


def test_collect_strips_tenant_prefix_and_detects_conflicts(tmp_path) -> None:
    first = tmp_path / "pod-a" / "tenants" / "tenant-a"
    second = tmp_path / "pod-b" / "tenants" / "tenant-a"
    (first / "skills" / "alpha").mkdir(parents=True)
    (second / "skills" / "alpha").mkdir(parents=True)
    (first / "skills" / "alpha" / "SKILL.md").write_text("first")
    (second / "skills" / "alpha" / "SKILL.md").write_text("second")

    objects, conflicts = collect_control_plane_objects(
        [tmp_path / "pod-a", tmp_path / "pod-b"],
        tenant_id="tenant-a",
    )

    assert objects["skills/alpha/SKILL.md"] == b"first"
    assert conflicts[0]["key"] == "skills/alpha/SKILL.md"


def test_migration_dry_run_and_apply(tmp_path) -> None:
    source = tmp_path / "pod" / "tenants" / "tenant-a"
    (source / "skills" / "alpha").mkdir(parents=True)
    (source / "skills" / "alpha" / "SKILL.md").write_text("skill")
    (source / "manifest.json").write_text('{"name":"alpha"}\n')
    (source / "sessions").mkdir()
    (source / "sessions" / "ignored.json").write_text("{}")
    target = TransactionalMemoryStore()

    preview = migrate_local_control_plane(
        [tmp_path / "pod"],
        target,
        tenant_id="tenant-a",
        apply=False,
    )
    assert preview["pending"] == 2
    assert preview["applied"] is False

    result = migrate_local_control_plane(
        [tmp_path / "pod"],
        target,
        tenant_id="tenant-a",
        apply=True,
    )

    assert result["applied"] is True
    assert target.get_object("skills/alpha/SKILL.md").read() == b"skill"
    marker = json.loads(target.get_object(result["marker_key"]).read())
    assert marker["verified"] is True
    assert "sessions/ignored.json" not in target._data


def test_target_conflict_stops_migration(tmp_path) -> None:
    source = tmp_path / "pod"
    source.mkdir()
    (source / "manifest.json").write_text("source")
    target = TransactionalMemoryStore()
    target.put_object("manifest.json", b"target")

    result = migrate_local_control_plane(
        [source],
        target,
        tenant_id="default",
        apply=True,
    )

    assert result["applied"] is False
    assert result["conflicts"][0]["key"] == "manifest.json"
    assert target.get_object("manifest.json").read() == b"target"
