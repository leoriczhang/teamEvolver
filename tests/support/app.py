"""API replica and tenant registry test collaborators."""

from __future__ import annotations

from team_skills.library.cache import SkillRuntimeCache
from teamEvolver.config_runtime import ConfigRepository


class _Registry:
    mode = "postgres"
    default_machine_credential_configured = False

    def __init__(self) -> None:
        from teamEvolver.tenants.registry import TenantContext

        self.tenants = {
            "default": TenantContext("default", "default"),
            "alpha": TenantContext("alpha", "alpha", config_overrides={"sharing_viking_account": "acct-alpha"}),
        }

    def get(self, tid):
        return self.tenants.get(tid)

    def list_tenants(self):
        return list(self.tenants.values())

    def default_context(self):
        return self.tenants["default"]

    def resolve_by_agent_token(self, token):
        return self.tenants["alpha"] if token == "tevt_alpha" else None


def _pod(pg_env, tmp_path, monkeypatch, *, pod: str, cache=None):
    from fastapi.testclient import TestClient

    from teamEvolver.admin.console_session import ConsoleSessionManager
    from teamEvolver.app import create_app
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.runtime.services import build_services

    monkeypatch.delenv("TEAMEVOLVER_ROOT_API_KEY", raising=False)
    monkeypatch.delenv("TEAMEVOLVER_PLATFORM_ADMINS", raising=False)
    config = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),  # shared identity store across pods
        skills_runtime_cache_dir=str(tmp_path / f"cache-{pod}"),
        sharing_skill_backend="local",
        sharing_local_root=str(tmp_path / "session"),
        sharing_skill_local_root=str(tmp_path / "legacy-skill"),
        sharing_viking_api_key="",
        sharing_viking_account="acct-default",
    )
    extras = {
        "config": config,
        "console_sessions": ConsoleSessionManager(config),
        "tenant_registry": _Registry(),
        "config_repo": ConfigRepository(pg_env.tx),
        "skill_runtime_cache": cache or SkillRuntimeCache(tmp_path / f"cache-{pod}"),
    }
    services = build_services(role="api", tx=pg_env.tx, extras=extras)
    return TestClient(create_app(services=services), base_url="http://192.0.2.10:1080")


def _login(client, *, bootstrap: bool):
    path = "/api/auth/bootstrap" if bootstrap else "/api/auth/login"
    resp = client.post(path, json={"username": "root", "password": "test-password"})
    assert resp.status_code == 200, resp.text


H = {"X-Tenant-Id": "default"}
