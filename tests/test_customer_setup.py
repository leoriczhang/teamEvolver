import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")
def test_prepare_and_import_reuse_root_key(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    (legacy / "converters").mkdir()
    project = "fixture-" + uuid.uuid4().hex[:10]
    (legacy / "config" / (project + ".yaml")).write_text(
        yaml.safe_dump(
            {
                "langfuse": {"host": "http://offline.test", "public_key": "pk", "secret_key": "sk"},
                "llm": {"url": "http://offline.test/v1", "model": "fixture", "token": "test"},
            }
        )
    )
    (legacy / "converters" / (project + ".py")).write_text(
        "from core.langfuse_client import langfuse_to_template\ndef convert(raw): return langfuse_to_template(raw)\n"
    )
    runtime = tmp_path / "runtime"
    env = {**os.environ, "TEAMEVOLVER_PG_DSN": os.environ["TE_PG_TEST_DSN"]}
    env.pop("TEAMEVOLVER_ROOT_API_KEY", None)
    env.pop("TEAMEVOLVER_CONFIG_FILE", None)
    command = [
        sys.executable,
        "scripts/prepare_customer.py",
        "--legacy-root",
        str(legacy),
        "--converters-dir",
        str(legacy / "converters"),
        "--runtime-dir",
        str(runtime),
    ]
    root = Path(__file__).resolve().parents[1]
    subprocess.run(command, env=env, cwd=root, capture_output=True, check=True, timeout=30)

    def read_env():
        return dict(
            token.split("=", 1) for token in shlex.split((runtime / "customer.env").read_text()) if "=" in token
        )

    first = read_env()
    assert len(first["TEAMEVOLVER_ROOT_API_KEY"]) >= 32
    subprocess.run(command + ["--apply"], env=env, cwd=root, capture_output=True, check=True, timeout=30)
    assert read_env()["TEAMEVOLVER_ROOT_API_KEY"] == first["TEAMEVOLVER_ROOT_API_KEY"]
    report = json.loads((runtime / "migration-report.json").read_text())
    assert report[0]["applied"] is True
    assert report[0]["converter"]["status"] == "compatible"
    assert (runtime / "customer.env").stat().st_mode & 0o777 == 0o600


def test_bootstrap_config_is_writable_runtime_file(monkeypatch, tmp_path):
    from teamEvolver.customer import create_app
    from teamEvolver.config_store import ConfigStore

    config_file = tmp_path / "state" / "config.yaml"
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_BOOTSTRAP", "1")
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-" + "a" * 32)
    monkeypatch.setattr("teamEvolver.proxy.server.ProxyServer", lambda config: type("Server", (), {"app": config})())
    config = create_app()
    assert config_file.exists()
    assert config._config_file == str(config_file)
    assert config.storage_pg_enabled
    store = ConfigStore(config_file)
    store.set("team.display_name", "retained")
    create_app()
    assert store.get("team.display_name") == "retained"
