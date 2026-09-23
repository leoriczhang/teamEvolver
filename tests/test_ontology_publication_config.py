"""No publication key files; TE derives backend credentials and identity itself."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from team_ontology.config import OntologyConfig


def test_old_yaml_keys_are_accepted_but_environment_signing_keys_are_unused(monkeypatch):
    monkeypatch.setenv("TE_ONTOLOGY_ENABLED", "1")
    monkeypatch.setenv("TE_ONTOLOGY_SIGNING_KEY_FILE", "/must/not/be/read")
    cfg = OntologyConfig.from_host(SimpleNamespace(ontology={"signing_key_id": "historical"}))
    assert cfg.enabled and cfg.signing_key_file == ""
    assert cfg.signing_key_id == "historical"


@pytest.mark.asyncio
async def test_native_resolver_uses_backend_root_not_personal_credentials(monkeypatch):
    import team_ontology.api as api
    import teamEvolver.proxy.tenant_routes as routes
    import teamEvolver.proxy.users_admin as users
    import teamEvolver.tenants.registry as tenants
    from team_ontology.api import install_native

    cfg = SimpleNamespace(
        ontology={"enabled": True},
        storage_pg_dsn="postgresql://unused",
        storage_pg_schema="custom_te",
        sharing_viking_api_key="backend-root",
        sharing_viking_team_api_key="team-key",
        sharing_viking_endpoint="http://ov",
        sharing_viking_account="mapped-account",
        llm_model_id="model",
        llm_api_base="http://model",
        llm_api_key="model-key",
    )
    app = FastAPI()
    app.state.owner = SimpleNamespace(config=cfg)
    registry = SimpleNamespace(get=lambda tenant: SimpleNamespace(status="active"))
    monkeypatch.setattr(routes, "get_tenant_registry", lambda owner: registry)
    monkeypatch.setattr(tenants, "effective_config", lambda *args: cfg)
    monkeypatch.setattr(users, "_registry_path", lambda config: "unused")
    monkeypatch.setattr(
        users,
        "_load_registry",
        lambda *args: {
            "users": [
                {
                    "id": "frank",
                    "role": "admin",
                    "team_space": {"viking_api_key": "personal-key", "viking_user": "mapped-frank"},
                }
            ]
        },
    )
    captured = {}
    monkeypatch.setattr(api, "install", lambda app, principal, **kwargs: captured.update(kwargs))
    install_native(app)
    assert captured["schema"] == "custom_te"
    value = await captured["resolver"]({"tenant": "te-tenant", "subject": "frank"})
    assert value["api_key"] == "backend-root"
    assert value["account"] == "mapped-account" and value["subject"] == "mapped-frank"
    cfg.sharing_viking_api_key = ""
    with pytest.raises(HTTPException, match="OV_CREDENTIAL_REQUIRED"):
        await captured["resolver"]({"tenant": "te-tenant", "subject": "frank"})
