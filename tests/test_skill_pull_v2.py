"""Skill pull acceptance: tenant isolation, validated incremental delivery and state."""

from types import SimpleNamespace
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.tenants.registry import TenantContext
from team_skills.library.hub import SkillHub
from team_skills.library.mutations import SkillMutationService


class Tenants:
    mode = "postgres"

    def default_context(self):
        return TenantContext("default")

    def resolve_by_agent_token(self, token):
        if token not in {"tevt_a", "tevt_b"}:
            return None
        suffix = token[-1]
        return TenantContext(f"tenant-{suffix}", config_overrides={"sharing_viking_account": f"account-{suffix}"})


def test_pull_auth_tenant_isolation_and_etag(tmp_path):
    config = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"), skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_skill_backend="local",
        sharing_skill_local_root=str(tmp_path / "store"),
        sharing_skill_mirror_enabled=False,
        sharing_viking_account="default-account",
    )
    for tenant in ("a", "b"):
        name = f"skill-{tenant}"
        SkillHub.team_from_config(
            config,
            tenant_id=f"tenant-{tenant}",
        ).publish_bundle(
            name,
            {
                "SKILL.md": (
                    f"---\nname: {name}\ndescription: Test\n---\nHello {tenant}"
                ).encode(),
            },
        )
    server = ProxyServer(config)
    server._tenant_registry = Tenants()
    http = TestClient(server.app)
    url = "/sync/skills?user_id=alice"
    assert http.get(url).status_code == 401
    assert http.get(url, headers={"Authorization": "Bearer tevt_invalid"}).status_code == 401
    a = {"Authorization": "Bearer tevt_a"}
    assert http.get("/sync/skills", headers=a).status_code == 400
    assert http.get("/sync/skills?user_id=../alice", headers=a).status_code == 400
    first = http.get(url, headers=a)
    assert first.status_code == 200
    assert [item["name"] for item in first.json()["skills"]] == ["skill-a"]
    assert http.get(url, headers={**a, "If-None-Match": first.headers["ETag"]}).status_code == 304
    other = http.get(url, headers={"Authorization": "Bearer tevt_b", "If-None-Match": first.headers["ETag"]})
    assert other.status_code == 200
    assert other.headers["ETag"] != first.headers["ETag"]
    assert [item["name"] for item in other.json()["skills"]] == ["skill-b"]
    assert http.get(url, headers={**a, "X-Tenant-Id": "tenant-b"}).status_code == 403
    assert http.get("/sync/skills/unexpected", headers=a).status_code == 403
    assert "/api/agent-protocol/metrics" not in {
        route.path for route in server.app.routes
    }


def test_empty_authoritative_registry_clears_runtime_cache(tmp_path):
    cache = tmp_path / "cache"
    stale = cache / "stale"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("---\nname: stale\n---\nold")
    hub = SkillHub(
        backend="local",
        endpoint="",
        local_root=str(tmp_path / "store"),
    )

    result = hub.materialize_runtime_cache(str(cache))

    assert result["total_remote"] == 0
    assert result["deleted"] == 1
    assert not stale.exists()


def test_publish_creates_rollout_event_only_when_rollout_is_enabled(tmp_path):
    hub = SkillHub(backend="local", endpoint="", local_root=str(tmp_path))
    config = SimpleNamespace(skillopt_rollout_enabled=False)
    service = SkillMutationService.from_hub(hub, config=config)

    commit = service.record_committed(
        action="publish",
        mutation_id="pull-only",
        expected={"name": "alpha", "version": 1},
        tenant_ids=[],
    )

    assert commit["status"] == "published"
    assert commit["delivery_mode"] == "pull"
    assert commit["event_id"] == ""
    assert list(hub._bucket.iter_objects("skill_rollout_outbox/")) == []

    config.skillopt_rollout_enabled = True
    rollout = service.record_committed(
        action="publish",
        mutation_id="rollout",
        expected={"name": "beta", "version": 1},
        tenant_ids=[],
    )

    assert rollout["event_id"].startswith("skill_rollout_evt_")
    events = list(service.iter_rollout_events())
    assert len(events) == 1
    assert events[0][1]["schema_version"] == "teamevolver.skill-rollout-outbox.v1"
