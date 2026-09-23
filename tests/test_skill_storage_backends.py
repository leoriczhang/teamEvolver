from __future__ import annotations

import pytest

from team_skills.candidates.store import ValidationStore
from team_skills.evolution.kernel.settings import EvolveServerConfig
from team_skills.evolution.runtime.orchestrator import EvolveServer
from team_skills.library.hub import SkillHub
from team_skills.library.mutations import SkillMutationService
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.proxy import routes
from teamEvolver.storage.local import LocalObjectStore
from teamEvolver.tenants.registry import TenantContext, effective_config


def _clear_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TEAMEVOLVER_PG_DSN",
        "TEAMEVOLVER_SKILL_STORAGE_BACKEND",
        "TEAMEVOLVER_SKILL_STORAGE_ROOT",
        "EVOLVE_STORAGE_BACKEND",
        "EVOLVE_SKILL_STORAGE_BACKEND",
        "EVOLVE_SKILL_STORAGE_LOCAL_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_config_store_keeps_pg_sessions_and_local_skill_storage(
    tmp_path, monkeypatch
):
    _clear_storage_env(monkeypatch)
    skill_root = tmp_path / "nas"
    store = ConfigStore(tmp_path / "config.yaml")
    store.save(
        {
            "sharing": {
                "enabled": True,
                "backend": "local",
                "session_backend": "local",
                "skill_backend": "local",
                "skill_local_root": str(skill_root),
            },
            "storage_pg": {
                "enabled": True,
                "dsn": "postgresql://user:pass@example.invalid/app",
            },
        }
    )

    config = store.to_config()
    assert config.sharing_backend == "local"
    assert config.sharing_session_backend == "postgres"
    assert config.sharing_skill_backend == "local"
    assert config.sharing_skill_local_root == str(skill_root)

    monkeypatch.setenv("TEAMEVOLVER_SKILL_STORAGE_BACKEND", "viking")
    monkeypatch.setenv(
        "TEAMEVOLVER_SKILL_STORAGE_ROOT", str(tmp_path / "override")
    )
    overridden = store.to_config()
    assert overridden.sharing_session_backend == "postgres"
    assert overridden.sharing_skill_backend == "viking"
    assert overridden.sharing_skill_local_root == str(tmp_path / "override")


@pytest.mark.parametrize("skill_backend", ["local", "viking"])
def test_effective_config_only_forces_pg_for_sessions(skill_backend):
    base = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn="postgresql://user:pass@example.invalid/app",
        sharing_session_backend="local",
        sharing_skill_backend=skill_backend,
        sharing_skill_local_root="/service/skill-storage",
    )

    config = effective_config(
        None,
        TenantContext(
            "tenant-a",
            config_overrides={
                "sharing_session_backend": "viking",
                "sharing_skill_backend": (
                    "viking" if skill_backend == "local" else "local"
                ),
                "sharing_skill_local_root": "/tenant/escape",
            },
        ),
        base,
    )

    assert config.sharing_session_backend == "postgres"
    assert config.sharing_skill_backend == skill_backend
    assert config.sharing_skill_local_root == "/service/skill-storage"


def test_evolve_config_preserves_split_storage(tmp_path, monkeypatch):
    _clear_storage_env(monkeypatch)
    config = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn="postgresql://user:pass@example.invalid/app",
        sharing_session_backend="postgres",
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path / "nas"),
    )

    evolve = EvolveServerConfig.from_teamEvolver_config(config)

    assert evolve.storage_backend == "postgres"
    assert evolve.skill_storage_backend == "local"
    assert evolve.skill_storage_local_root == str(tmp_path / "nas")


def test_local_skill_hub_archives_versions_and_scopes_tenant(tmp_path):
    storage_root = tmp_path / "nas"
    working_root = tmp_path / "working"
    skill_dir = working_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_skill_backend="local",
        sharing_skill_local_root=str(storage_root),
        sharing_skill_mirror_enabled=False,
        skills_delivery_mode="pull",
    )
    hub = SkillHub.team_from_config(config, tenant_id="tenant-a")

    skill_md.write_text(
        "---\nname: demo\ndescription: Demo\n---\nVersion one\n"
    )
    assert hub.push_skills(str(working_root))["uploaded"] == 1
    skill_md.write_text(
        "---\nname: demo\ndescription: Demo\n---\nVersion two\n"
    )
    assert hub.push_skills(str(working_root))["uploaded"] == 1

    prefix = storage_root / "tenants" / "tenant-a" / "skills" / "demo"
    assert (prefix / "versions" / "v1" / "SKILL.md").read_text().endswith(
        "Version one\n"
    )
    assert (prefix / "versions" / "v2" / "SKILL.md").read_text().endswith(
        "Version two\n"
    )
    assert (prefix / "SKILL.md").read_text().endswith("Version two\n")

    validation = ValidationStore.from_config(config, tenant_id="tenant-a")
    validation.save_job({"job_id": "job-1"})
    assert (
        storage_root
        / "tenants"
        / "tenant-a"
        / "validation_jobs"
        / "job-1.json"
    ).is_file()

    mutations = SkillMutationService.from_config(config, tenant_id="tenant-a")
    mutations.record_committed(
        action="publish",
        mutation_id="mutation-1",
        expected={"name": "demo", "version": 2},
        tenant_ids=[],
    )
    assert (
        storage_root
        / "tenants"
        / "tenant-a"
        / "skill_mutation_commits"
        / "mutation-1.json"
    ).is_file()


def test_viking_skill_hub_archives_versions_and_scopes_tenant(tmp_path):
    working_root = tmp_path / "working"
    skill_dir = working_root / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_skill_backend="viking",
        sharing_viking_endpoint=f"memory://{tmp_path.name}",
        sharing_skill_mirror_enabled=False,
    )
    hub = SkillHub.team_from_config(config, tenant_id="tenant-a")

    skill_md.write_text("---\nname: demo\n---\nViking version one\n")
    assert hub.push_skills(str(working_root))["uploaded"] == 1
    skill_md.write_text("---\nname: demo\n---\nViking version two\n")
    assert hub.push_skills(str(working_root))["uploaded"] == 1

    prefix = "tenants/tenant-a/skills/demo"
    assert hub._bucket.get_object(
        f"{prefix}/versions/v1/SKILL.md"
    ).read().endswith(b"Viking version one\n")
    assert hub._bucket.get_object(
        f"{prefix}/versions/v2/SKILL.md"
    ).read().endswith(b"Viking version two\n")
    assert hub._bucket.get_object(f"{prefix}/SKILL.md").read().endswith(
        b"Viking version two\n"
    )


def test_evolution_upload_uses_local_skill_store_and_tenant_prefix(tmp_path):
    config = EvolveServerConfig(
        storage_backend="local",
        storage_local_root=str(tmp_path / "session-state"),
        skill_storage_backend="local",
        skill_storage_local_root=str(tmp_path / "nas"),
        pg_tenant_id="tenant-a",
        skill_mirror_enabled=False,
    )
    server = EvolveServer(config)

    assert isinstance(server._skill_bucket, LocalObjectStore)
    assert server._skill_prefix == "tenants/tenant-a/"
    assert (
        server._upload_skill(
            {
                "name": "evolved-demo",
                "description": "Demo",
                "content": "Version one",
            },
            "create_skill",
        )
        == "uploaded"
    )
    assert (
        server._upload_skill(
            {
                "name": "evolved-demo",
                "description": "Demo",
                "content": "Version two",
            },
            "update_skill",
        )
        == "uploaded"
    )

    prefix = tmp_path / "nas" / "tenants" / "tenant-a"
    versions = prefix / "skills" / "evolved-demo" / "versions"
    assert (versions / "v1" / "SKILL.md").is_file()
    assert (versions / "v2" / "SKILL.md").is_file()
    assert list((prefix / "skill_mutation_commits").glob("*.json"))


def test_storage_status_reports_local_skills_and_pg_sessions(
    tmp_path, monkeypatch
):
    class FakePgObjectStore:
        def pool_status(self):
            return {"reachable": True, "pool_size": 2}

    class FakeSessionHub:
        _bucket = FakePgObjectStore()

    monkeypatch.setattr(routes, "PgObjectStore", FakePgObjectStore)
    monkeypatch.setattr(
        SkillHub,
        "object_storage_from_config",
        lambda config, tenant_id="default": FakeSessionHub(),
    )
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path / "nas"),
        sharing_session_backend="postgres",
        sharing_skill_mirror_enabled=False,
    )

    status = routes._storage_status(config)

    assert status["reachable"] is True
    assert status["effective_backend"] == "local"
    assert status["skill_backend"] == "local"
    assert status["session_backend"] == "postgres"
    assert status["pg"]["reachable"] is True


def test_empty_skill_backend_defaults_to_local_not_viking(tmp_path):
    """Per-purpose empty → local, even when sharing_backend=viking + endpoint.

    Regression: _build used sharing_local_root="" as a "remote-Agent pull"
    signal and inherited sharing_backend=viking, so teamEvolver's own service
    read candidates from viking (404) while the evolution engine wrote them
    locally — candidates were invisible.
    """
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_viking_endpoint=f"memory://{tmp_path.name}",
        sharing_skill_backend="",
        sharing_local_root="",
        sharing_skill_mirror_enabled=False,
    )
    hub = SkillHub.team_from_config(config, tenant_id="tenant-a")
    assert isinstance(hub._bucket, LocalObjectStore)
