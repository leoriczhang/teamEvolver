"""Acceptance tests installed only in the final, strict release."""
import importlib.util
import json

from fastapi.testclient import TestClient
import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.config_store import ConfigStore
from teamEvolver.integrations.agent_principal import AgentPrincipal
from teamEvolver.integrations.context_workspace import ContextStateStore
from teamEvolver.proxy import ProxyServer
from team_skills.library.hub import SkillHub
from team_skills.library.mutations import SkillMutationService


def test_retired_interfaces_are_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "root-for-test")
    config = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"), skills_dir=str(tmp_path / "skills"),
                              sharing_enabled=False, sharing_skill_mirror_enabled=False)
    server = ProxyServer(config)
    client = TestClient(server.app)
    registered = {route.path for route in server.app.routes}
    for path in ("/internal/agents/register", "/internal/agentshub/openviking-config", "/api/agent-integrations",
                 "/api/agent-integrations/skill-sync/{event_id}/retry", "/api/agent-integrations/skill-sync/{event_id}/discard"):
        assert path not in registered
        assert client.post(path, json={}, headers={"Authorization": "Bearer root-for-test"}).status_code == 404
    for module in ("agent_registry", "legacy_agent_identity", "legacy_context_workspace", "skill_sync_adapters"):
        assert importlib.util.find_spec(f"teamEvolver.integrations.{module}") is None


def test_retired_configuration_cannot_reenable_legacy(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    store.save({"agent_protocol": {"identity_mode": "dual"}, "skills": {"delivery_mode": "push"},
                "replay": {"adapter": "customer.py"}})
    monkeypatch.setenv("TEAMEVOLVER_AGENT_IDENTITY_MODE", "dual")
    monkeypatch.setenv("TEAMEVOLVER_SKILLS_DELIVERY_MODE", "push")
    config = store.to_config()
    assert not hasattr(config, "agent_protocol_identity_mode")
    assert not hasattr(config, "skills_delivery_mode")
    assert config.replay_adapter == "customer.py"


def test_published_mutations_have_no_delivery_queue(tmp_path):
    hub = SkillHub(backend="local", endpoint="", local_root=str(tmp_path))
    service = SkillMutationService.from_hub(hub)
    commit = service.record_committed(action="publish", mutation_id="publish-1",
                                      expected={"name": "demo", "version": 1}, tenant_ids=["tenant-a"])
    assert commit["status"] == "published" and commit["event_id"] == ""
    assert not list(hub._bucket.iter_objects("skill_sync_outbox/"))
    for method in ("drain", "retry", "discard"):
        assert not hasattr(service, method)
    assert service.record_committed(action="publish", mutation_id="publish-1",
                                    expected={"name": "demo", "version": 1}, tenant_ids=["tenant-a"]) == commit


def test_context_requires_persisted_tenant_binding(tmp_path):
    config = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"))
    store = ContextStateStore(config)
    store.path.write_text(json.dumps({"sessions": {"old": {"user_id": "alice"}}}))
    assert store.get_session("old", principal=AgentPrincipal("default", "account", "alice")) is None


@pytest.mark.parametrize("token", ["", "tevt_wrong", "tev1_retired"])
def test_context_rejects_missing_invalid_or_retired_tokens(tmp_path, token):
    config = TeamEvolverConfig(users_registry_path=str(tmp_path / "users.json"), skills_dir=str(tmp_path / "skills"),
                              sharing_enabled=False, sharing_skill_mirror_enabled=False,
                              sharing_viking_account="account", tenant_machine_token="tevt_good")
    client = TestClient(ProxyServer(config).app)
    response = client.get("/internal/agents/context/describe?user_id=alice",
                          headers={"Authorization": f"Bearer {token}"} if token else {})
    assert response.status_code == 401
