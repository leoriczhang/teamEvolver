from tests.test_tenant_config_routes import _client


def test_project_connection_settings_do_not_change_global_config():
    client, owner, registry, _ = _client()
    ctx, _ = registry.create_tenant("project")
    url = f"/api/tenants/{ctx.tenant_id}/langfuse-config"
    old_host = owner.config.langfuse_host
    response = client.post(url, json={"host": "https://project.example", "enabled": True, "max_sessions": 25})
    assert response.status_code == 200, response.text
    assert client.get(url).json()["host"] == "https://project.example"
    assert owner.config.langfuse_host == old_host
    assert client.post(url, json={"max_sessions": 1001}).status_code == 400


def test_converter_preview_and_validation_require_admin():
    code = "from core.langfuse_client import langfuse_to_template\ndef convert(raw): return langfuse_to_template(raw)\n"
    client, _, registry, _ = _client()
    ctx, _ = registry.create_tenant("project")
    url = f"/api/tenants/{ctx.tenant_id}/converter"
    assert client.post(url + "/check", json={"code": code}).json()["status"] == "compatible"
    result = client.post(
        url + "/test",
        json={"code": code, "raw": {"trace": {"input": "question", "output": "answer"}, "observations": []}},
    )
    assert result.status_code == 200, result.text
    assert result.json()["turn"]["response_text"] == "answer"
    other, _, _, _ = _client(role="user")
    assert other.post(url + "/check", json={"code": code}).status_code == 403


def test_project_datasource_separates_source_from_conversion_mode():
    code = "from core.langfuse_client import langfuse_to_template\ndef convert(raw): return langfuse_to_template(raw)\n"
    client, _, registry, _ = _client()
    ctx, _ = registry.create_tenant("project")
    response = client.put(
        f"/api/tenants/{ctx.tenant_id}/config",
        json={
            "overrides": {
                "datasource_type": "skillopt",
                "datasource_legacy_converter_code": code,
            }
        },
    )
    assert response.status_code == 200, response.text

    datasource = client.get(
        f"/api/tenants/{ctx.tenant_id}/datasource-config"
    ).json()
    assert datasource["source"] == "langfuse"
    assert datasource["type"] == "skillopt"
    assert datasource["conversion_mode"] == "legacy_skillopt"
    assert datasource["legacy_converter_code"] == code


def test_bad_converter_cannot_be_saved():
    client, _, registry, _ = _client()
    ctx, _ = registry.create_tenant("project")
    response = client.put(
        f"/api/tenants/{ctx.tenant_id}/config",
        json={
            "overrides": {
                "datasource_type": "skillopt",
                "datasource_legacy_converter_code": "from core.engine import PROJECT_NAME\ndef convert(raw): return {}",
            }
        },
    )
    assert response.status_code == 400
    assert not registry.get(ctx.tenant_id).config_overrides
