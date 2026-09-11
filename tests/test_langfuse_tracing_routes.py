from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.observability import configure_langfuse
from teamEvolver.proxy.server import ProxyServer
from teamEvolver.tenants.registry import TenantContext


class _TenantRegistry:
    mode = "postgres"

    def __init__(self) -> None:
        self.tenant = TenantContext(
            tenant_id="account-a",
            display_name="Account A",
        )

    @staticmethod
    def default_context() -> TenantContext:
        return TenantContext()

    def get(self, tenant_id: str) -> TenantContext | None:
        return self.tenant if tenant_id == self.tenant.tenant_id else None


def _server(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "langfuse": {
                    "enabled": True,
                    "host": "https://tenant-source.example.com",
                    "public_key": "pk-source",
                    "secret_key": "sk-source",
                },
                "sharing": {
                    "enabled": False,
                    "skill_mirror_enabled": False,
                },
                "skills": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    root_key = "test-root-" + "x" * 40
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", root_key)
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setenv("TEAMEVOLVER_SKILLMINER_ENABLED", "0")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        "teamEvolver.proxy.server.sync_openviking_user",
        lambda *_args, **_kwargs: {"synced": False},
    )
    server = ProxyServer(ConfigStore().to_config())
    server._tenant_registry = _TenantRegistry()
    return (
        TestClient(server.app),
        config_file,
        {
            "Authorization": f"Bearer {root_key}",
            "X-Tenant-Id": "account-a",
        },
    )


def test_global_tracing_config_is_separate_from_source(
    monkeypatch,
    tmp_path,
) -> None:
    client, config_file, headers = _server(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/api/langfuse-tracing-config",
            headers=headers,
            json={
                "enabled": True,
                "host": "https://global-observability.example.com",
                "public_key": "pk-observability",
                "secret_key": "sk-observability",
                "environment": "production",
                "sample_rate": 0.25,
                "capture_content": False,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["host"] == "https://global-observability.example.com"
        assert body["public_key_present"] is True
        assert body["secret_key_present"] is True
        assert "public_key" not in body
        assert "secret_key" not in body

        persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        langfuse = persisted["langfuse"]
        assert langfuse["host"] == "https://tenant-source.example.com"
        assert langfuse["public_key"] == "pk-source"
        assert langfuse["secret_key"] == "sk-source"
        assert (
            langfuse["tracing_host"]
            == "https://global-observability.example.com"
        )
        assert langfuse["tracing_public_key"] == "pk-observability"
        assert langfuse["tracing_secret_key"] == "sk-observability"

        source = client.post(
            "/api/langfuse-config",
            headers={"Authorization": headers["Authorization"]},
            json={
                "enabled": True,
                "host": "https://new-tenant-source.example.com",
                "public_key": "pk-new-source",
                "secret_key": "sk-new-source",
            },
        )
        assert source.status_code == 200, source.text
        persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        langfuse = persisted["langfuse"]
        assert langfuse["host"] == "https://new-tenant-source.example.com"
        assert (
            langfuse["tracing_host"]
            == "https://global-observability.example.com"
        )
        assert langfuse["tracing_public_key"] == "pk-observability"
        assert langfuse["tracing_secret_key"] == "sk-observability"
    finally:
        configure_langfuse(TeamEvolverConfig())


def test_global_tracing_requires_its_own_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    client, _config_file, headers = _server(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/api/langfuse-tracing-config",
            headers=headers,
            json={
                "enabled": True,
                "host": "https://global-observability.example.com",
            },
        )
        assert response.status_code == 400
        assert "public_key" in response.json()["detail"]
    finally:
        configure_langfuse(TeamEvolverConfig())
