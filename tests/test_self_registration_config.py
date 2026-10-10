"""Self-registration is a process-wide policy, with an explicit environment override."""
from pathlib import Path

import pytest
import yaml

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.tenants.registry import apply_tenant_config_overrides


@pytest.mark.parametrize("yaml_value,env_value,expected", [
    (None, None, True), (False, None, False), (True, None, True),
    (True, "0", False), (False, "1", True), (True, "false", False), (False, "true", True),
])
def test_policy_config_precedence(tmp_path, monkeypatch, yaml_value, env_value, expected):
    monkeypatch.setenv("TEAMEVOLVER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("TEAMEVOLVER_SELF_REGISTRATION_ENABLED", raising=False)
    if env_value is not None:
        monkeypatch.setenv("TEAMEVOLVER_SELF_REGISTRATION_ENABLED", env_value)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"service": {} if yaml_value is None else {
        "self_registration_enabled": yaml_value,
    }}))
    assert ConfigStore(path).to_config().self_registration_enabled is expected


def test_invalid_policy_cannot_silently_enable_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("TEAMEVOLVER_SELF_REGISTRATION_ENABLED", "flase")
    with pytest.raises(ValueError, match="self_registration_enabled"):
        ConfigStore(tmp_path / "config.yaml").to_config()


def test_tenants_cannot_reenable_public_registration():
    config = TeamEvolverConfig(self_registration_enabled=False)
    assert not apply_tenant_config_overrides(config, {"self_registration_enabled": True}).self_registration_enabled


def test_production_template_disables_registration():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config/config_prd.yaml").read_text())
    assert config["service"]["self_registration_enabled"] is False
