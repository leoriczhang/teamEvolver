import importlib.util
import json
import os

import httpx

from tests.support.paths import REPO_ROOT


def script():
    spec = importlib.util.spec_from_file_location("connect_ma", REPO_ROOT / "scripts/connect_managed_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generic_connection_reuses_tenant_key_and_official_revision_api(monkeypatch):
    module = script()
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "root")
    monkeypatch.setenv("NOTE_MA_KEY", "ma-private")
    calls = []

    def response(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, path, body))
        if path == "/api/tenants":
            assert request.method == "GET"
            return httpx.Response(200, json={"tenants": [{"tenant_id": "notes", "status": "active"}]})
        if path == "/api/ma/connections/notes":
            return httpx.Response(200, json={"revision": 3})
        return httpx.Response(202, json={"job_id": "discovery", "status_url": "/api/jobs/discovery"})

    console = module.Console("https://team.test",
                             httpx.Client(base_url="https://team.test", transport=httpx.MockTransport(response)))
    spec = {"tenant": {"id": "notes"}, "connection_id": "notes", "secret_env": {
        "ma_api_key": "NOTE_MA_KEY", "tenant_token": "NOTE_TENANT_KEY",
    }}
    result = console.apply(spec, {"display_name": "Notes", "entry_agent_id": "editor"})
    assert result["key_rotated"] is False
    update = next(body for method, path, body in calls if method == "PUT")
    assert update["expected_revision"] == 3 and update["ma_api_key"] == "ma-private"
    assert "tenant_token" not in update
    assert all("rotate-token" not in path for _, path, _ in calls)
    console.client.close()


def test_explicit_env_file_is_data_not_shell_and_private_output(monkeypatch, tmp_path):
    module = script()
    path = tmp_path / "credentials.env"
    path.write_text('NOTE_KEY="literal-$(echo nope)"\n')
    monkeypatch.delenv("NOTE_KEY", raising=False)
    module.load_env(path)
    assert os.environ["NOTE_KEY"] == "literal-$(echo nope)"
    module.persist_environment(path, {"TENANT_KEY": "tevt_private"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert "NOTE_KEY" in path.read_text()
    config = tmp_path / "deployment.json"
    config.write_text(json.dumps({"agents": {"editor": {"id": "agent"}}}))
    assert module.resolve({"$json": str(config), "pointer": "/agents/editor/id"}) == "agent"
