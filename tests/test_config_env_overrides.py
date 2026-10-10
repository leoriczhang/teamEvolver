"""Env overrides applied by ConfigStore.to_config()."""

import pytest

from teamEvolver.config_store.bridge import ConfigStore


@pytest.fixture(autouse=True)
def _clear_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TEAMEVOLVER_PG_DSN", "TEAMEVOLVER_PG_SCHEMA"):
        monkeypatch.delenv(name, raising=False)


def test_pg_schema_env_overrides_config_file(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "storage_pg:\n  enabled: true\n  schema: teamevolver\n",
        encoding="utf-8",
    )
    store = ConfigStore(config_file=config)
    assert store.to_config().storage_pg_schema == "teamevolver"
    monkeypatch.setenv("TEAMEVOLVER_PG_SCHEMA", "teamevolver_dev_01444803")
    assert store.to_config().storage_pg_schema == "teamevolver_dev_01444803"
