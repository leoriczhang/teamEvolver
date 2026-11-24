from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import teamEvolver.app as application
import teamEvolver.runtime.entrypoint as entrypoint
from teamEvolver.config import TeamEvolverConfig


@pytest.mark.parametrize("role", ["worker", "api", "all"])
def test_runtime_honors_configured_database_pool_limits(monkeypatch, role):
    pool = SimpleNamespace(schema="test_schema")
    factory = Mock(return_value=pool)
    monkeypatch.setattr(entrypoint, "get_database_pool", factory)
    monkeypatch.setattr(application, "build_role_services", Mock(return_value=object()))
    monkeypatch.setattr(application, "create_app", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "build_lifecycle", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "build_health_app", Mock(return_value=object()))
    monkeypatch.setattr(entrypoint, "_serve_uvicorn", AsyncMock())
    config = TeamEvolverConfig(
        storage_pg_dsn="postgresql://localhost/example", storage_pg_schema="test_schema",
        storage_pg_pool_min=1, storage_pg_pool_max=3, storage_pg_command_timeout_seconds=12,
    )
    entrypoint.run_role(role=role, config=config)
    factory.assert_called_once_with(
        dsn=config.storage_pg_dsn, schema="test_schema", ssl=config.storage_pg_ssl,
        pool_min=1, pool_max=3, command_timeout=12,
    )


# Tenant/lane concurrency is exercised with the task repository in
# test_tenant_isolation.py rather than mocking away the worker loop here.
