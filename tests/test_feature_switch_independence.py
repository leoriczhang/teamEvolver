from dataclasses import fields
from pathlib import Path

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from team_skills.candidates.worker import ValidationWorker
from team_skills.evolution.patterns import ExperiencePatternStore
from team_skills.library.experience_sync import Target
from team_skills.library.hub import SkillHub


def test_legacy_sharing_switch_is_accepted_but_not_exposed(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("sharing:\n  enabled: false\n", encoding="utf-8")

    config = ConfigStore(config_file).to_config()

    assert not hasattr(config, "sharing_enabled")


def test_runtime_config_exposes_only_current_sharing_credentials_and_backends():
    sharing_fields = {
        field.name
        for field in fields(TeamEvolverConfig)
        if field.name.startswith("sharing_")
    }
    assert {
        "sharing_skill_backend",
        "sharing_session_backend",
        "sharing_viking_api_key",
    } <= sharing_fields
    assert {
        name for name in sharing_fields if name.endswith("_api_key")
    } == {"sharing_viking_api_key"}


def test_validation_uses_its_own_switch():
    worker = object.__new__(ValidationWorker)
    worker.config = TeamEvolverConfig(validation_enabled=True)

    assert worker._validation_enabled() is True


def test_openviking_features_use_their_own_requirements(tmp_path: Path):
    config = TeamEvolverConfig(
        storage_pg_enabled=True,
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path),
        sharing_skill_mirror_enabled=True,
        sharing_viking_endpoint="http://openviking.test:1933",
        sharing_viking_api_key="root-key",
        sharing_viking_account="account",
        sharing_viking_user="team",
        experience_sync={"enabled": True},
    )

    assert Target.from_config(config).endpoint == "http://openviking.test:1933"
    assert ExperiencePatternStore.from_config(config).location.endswith("/patterns")
    assert SkillHub.team_from_config(config).mirror_viking_hub is not None
