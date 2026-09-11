from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from teamEvolver.config_store import ConfigStore
from teamEvolver.proxy.server import ProxyServer
from teamEvolver.tenants.registry import TenantContext


class _TenantRegistry:
    mode = "postgres"

    def __init__(self, tenant: TenantContext) -> None:
        self._tenant = tenant

    @staticmethod
    def default_context() -> TenantContext:
        return TenantContext()

    def get(self, tenant_id: str) -> TenantContext | None:
        return self._tenant if tenant_id == self._tenant.tenant_id else None


def test_sharing_config_uses_environment_overrides_for_navigation_gate(
    monkeypatch,
    tmp_path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "sharing": {
                    "enabled": True,
                    "skill_mirror_enabled": False,
                    "viking_deployment": "local",
                    "viking_endpoint": "",
                },
                "skills": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    root_key = "test-root-" + "x" * 40
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", root_key)
    monkeypatch.setenv("TEAMEVOLVER_OV_ENDPOINT", "http://openviking.test:1933")
    monkeypatch.setenv("TEAMEVOLVER_OV_ROOT_KEY", "openviking-root-key")
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    monkeypatch.setenv("TEAMEVOLVER_SKILLMINER_ENABLED", "0")
    monkeypatch.setattr(
        "teamEvolver.proxy.server.sync_openviking_user",
        lambda *_args, **_kwargs: {"synced": False},
    )

    server = ProxyServer(ConfigStore().to_config())
    tenant = TenantContext(tenant_id="account-a", display_name="Account A")
    server._tenant_registry = _TenantRegistry(tenant)

    with TestClient(server.app) as client:
        response = client.get(
            "/api/sharing-config",
            headers={
                "Authorization": f"Bearer {root_key}",
                "X-Tenant-Id": tenant.tenant_id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["endpoint"] == "http://openviking.test:1933"
    assert body["service_api_key_present"] is True
    assert body["account_bound"] is True
