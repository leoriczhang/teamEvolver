import asyncio
from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient

from team_skills.evolution import prompt_studio
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.llm import AsyncLLMClient
from teamEvolver.proxy import ProxyServer
from teamEvolver.storage import admin_kv
from teamEvolver.tenants.registry import (
    TenantContext,
    reset_current_tenant,
    set_current_tenant,
)


class _TenantRegistry:
    mode = "postgres"
    runtime = None

    def __init__(self):
        self.ctx = TenantContext(
            tenant_id="tenant-a",
            display_name="Tenant A",
            status="active",
            config_overrides={
                "llm_provider": "custom",
                "llm_api_base": "https://tenant.example/v1",
                "llm_api_key": "tenant-secret",
                "llm_model_id": "tenant-model",
                "llm_max_tokens": 16384,
                "llm_temperature": 0.2,
            },
        )

    def default_context(self):
        return TenantContext(tenant_id="default", display_name="Default")

    def get(self, tenant_id):
        return self.ctx if tenant_id == self.ctx.tenant_id else None

    def update_tenant_config(self, tenant_id, overrides):
        if tenant_id != self.ctx.tenant_id:
            return None
        merged = dict(self.ctx.config_overrides)
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self.ctx = TenantContext(
            tenant_id=self.ctx.tenant_id,
            display_name=self.ctx.display_name,
            status=self.ctx.status,
            config_overrides=merged,
        )
        return self.ctx


def _server(tmp_path):
    config = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        users_registry_path=str(tmp_path / "users.json"),
        skills_dir=str(tmp_path / "skills"),
        sharing_enabled=False,
        sharing_skill_mirror_enabled=False,
        llm_api_base="https://global.example/v1",
        llm_api_key="global-secret",
        llm_model_id="global-model",
    )
    server = ProxyServer(config)
    server._tenant_registry = _TenantRegistry()
    server._get_engine_pool = lambda: None
    server._stop_skillminer = lambda: None
    return server


def test_tenant_model_settings_are_isolated_and_secrets_are_masked(tmp_path):
    server = _server(tmp_path)
    client = TestClient(server.app)
    bootstrap = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "test-password"},
    )
    assert bootstrap.status_code == 200
    headers = {"X-Tenant-Id": "tenant-a"}

    current = client.get("/api/model-settings", headers=headers)
    assert current.status_code == 200
    assert current.json() == {
        "tenant_id": "tenant-a",
        "scope": "tenant",
        "provider": "custom",
        "base_url": "https://tenant.example/v1",
        "model": "tenant-model",
        "max_tokens": 16384,
        "temperature": 0.2,
        "api_key_present": True,
    }

    saved = client.post(
        "/api/model-settings",
        headers=headers,
        json={
            "provider": "custom",
            "base_url": "https://other.example/v1",
            "model": "other-model",
            "api_key": "other-secret",
            "max_tokens": 32768,
            "temperature": 0.5,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["model"] == "other-model"
    assert "api_key" not in saved.json()
    assert server.config.llm_model_id == "global-model"
    assert server._tenant_registry.ctx.config_overrides["llm_api_key"] == "other-secret"

    tenant_config = client.get("/api/tenants/tenant-a/config")
    assert tenant_config.status_code == 200
    assert "llm_api_key" not in tenant_config.json()["config_overrides"]
    assert tenant_config.json()["secret_presence"]["llm_api_key"] is True


def test_stage_settings_keep_secret_private_and_support_full_connection(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TEAMEVOLVER_STAGE_SETTINGS_PATH",
        str(tmp_path / "stage-settings.json"),
    )
    config = TeamEvolverConfig(storage_pg_enabled=False)

    prompt_studio.set_stage_settings(
        "analyze_session",
        {
            "provider": "custom",
            "base_url": "https://stage.example/v1/",
            "model": "stage-model",
            "api_key": "stage-secret",
            "temperature": 0.6,
            "max_tokens": 1234,
        },
        config=config,
    )

    detail = prompt_studio.get_prompt("analyze_session")
    assert detail["provider"] == "custom"
    assert detail["base_url"] == "https://stage.example/v1"
    assert detail["model"] == "stage-model"
    assert detail["api_key_present"] is True
    assert "api_key" not in detail

    options = prompt_studio.stage_call_options("analyze_session")
    assert options["api_key"] == "stage-secret"
    assert options["base_url"] == "https://stage.example/v1"

    prompt_studio.set_stage_settings(
        "analyze_session",
        {
            "provider": "custom",
            "base_url": "https://stage.example/v1",
            "model": "stage-model-v2",
            "temperature": 0.4,
            "max_tokens": 2048,
        },
        config=config,
    )
    assert prompt_studio.stage_call_options("analyze_session")["api_key"] == "stage-secret"

    prompt_studio.set_stage_settings(
        "analyze_session",
        {
            "model": "",
            "clear_api_key": True,
            "temperature": 0.1,
            "max_tokens": 32768,
        },
        config=config,
    )
    assert "api_key" not in prompt_studio.stage_call_options("analyze_session")


def test_stage_settings_are_isolated_by_tenant(tmp_path, monkeypatch):
    objects: dict[tuple[str, str], bytes] = {}

    class _Store:
        def __init__(self, tenant_id):
            self.tenant_id = tenant_id

        def get_object(self, key):
            try:
                payload = objects[(self.tenant_id, key)]
            except KeyError as exc:
                raise FileNotFoundError(key) from exc
            return BytesIO(payload)

        def put_object(self, key, payload):
            objects[(self.tenant_id, key)] = payload

    monkeypatch.setattr(
        admin_kv,
        "_pg_store",
        lambda _config, tenant_id: _Store(tenant_id),
    )
    config = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn="postgresql://unused",
    )

    token_a = set_current_tenant(TenantContext("tenant-a"))
    try:
        prompt_studio.set_stage_settings(
            "create_skill",
            {
                "base_url": "https://a.example/v1",
                "model": "model-a",
                "api_key": "key-a",
                "temperature": 0.3,
                "max_tokens": 4096,
            },
            config=config,
        )
    finally:
        reset_current_tenant(token_a)

    token_b = set_current_tenant(TenantContext("tenant-b"))
    try:
        assert prompt_studio.stage_call_options("create_skill", config) == {
            "temperature": 0.4,
            "max_tokens": 16384,
        }
        prompt_studio.set_stage_settings(
            "create_skill",
            {
                "base_url": "https://b.example/v1",
                "model": "model-b",
                "api_key": "key-b",
                "temperature": 0.7,
                "max_tokens": 8192,
            },
            config=config,
        )
    finally:
        reset_current_tenant(token_b)

    token_a = set_current_tenant(TenantContext("tenant-a"))
    try:
        options_a = prompt_studio.stage_call_options("create_skill", config)
    finally:
        reset_current_tenant(token_a)
    token_b = set_current_tenant(TenantContext("tenant-b"))
    try:
        options_b = prompt_studio.stage_call_options("create_skill", config)
    finally:
        reset_current_tenant(token_b)

    assert options_a["model"] == "model-a"
    assert options_a["api_key"] == "key-a"
    assert options_b["model"] == "model-b"
    assert options_b["api_key"] == "key-b"


def test_llm_client_uses_stage_endpoint_and_key(monkeypatch):
    calls = []

    class _Completions:
        def __init__(self, owner):
            self.owner = owner

        def create(self, **kwargs):
            calls.append((self.owner.api_key, self.owner.base_url, kwargs))
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok"),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

    class _FakeOpenAI:
        def __init__(self, *, api_key, base_url, **_kwargs):
            self.api_key = api_key
            self.base_url = str(base_url).rstrip("/")
            self.chat = SimpleNamespace(completions=_Completions(self))

    import openai

    from teamEvolver import llm as llm_module

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    async def _direct(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(llm_module, "_call_in_pool", _direct)
    client = AsyncLLMClient(
        api_key="default-key",
        base_url="https://default.example/v1",
        model="default-model",
        max_retries=1,
    )
    result = asyncio.run(
        client.chat(
            [{"role": "user", "content": "test"}],
            provider="custom",
            base_url="https://stage.example/v1/",
            api_key="stage-key",
            model="stage-model",
        )
    )

    assert result == "ok"
    assert calls[0][0] == "stage-key"
    assert calls[0][1] == "https://stage.example/v1"
    assert calls[0][2]["model"] == "stage-model"
    assert "api_key" not in calls[0][2]
    assert "base_url" not in calls[0][2]
    assert "provider" not in calls[0][2]
