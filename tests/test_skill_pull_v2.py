"""Skill pull acceptance: tenant isolation, validated incremental delivery and state."""

import asyncio
import base64
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations.hermes_skill_sync import sync_skills as client
from teamEvolver.proxy import ProxyServer
from teamEvolver.tenants.registry import TenantContext
from team_skills.library.bundle import bundle_tree_sha256
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
        users_registry_path=str(tmp_path / "users.json"), skills_dir=str(tmp_path / "skills"),
        sharing_enabled=False, sharing_skill_mirror_enabled=False, sharing_viking_account="default-account",
    )
    for tenant in ("a", "b"):
        skill = tmp_path / "skills" / "tenants" / f"tenant-{tenant}" / f"skill-{tenant}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: skill-{tenant}\ndescription: Test\n---\nHello {tenant}")
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
    assert http.get("/api/agent-protocol/metrics", headers=a).status_code in {401, 403}


def bundle(name="test", text="hello", version=1):
    import hashlib

    files = {"SKILL.md": text.encode(), "reference.txt": b"reference"}
    return {
        "name": name, "version": version, "sha256": hashlib.sha256(files["SKILL.md"]).hexdigest(),
        "tree_sha256": bundle_tree_sha256(files),
        "files": [{"path": path, "content_b64": base64.b64encode(data).decode()} for path, data in files.items()],
    }


def test_client_etag_no_rewrite_changed_bundle_and_repair(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    cfg = {"base_url": "http://example.invalid", "user_id": "alice", "tenant_token": "tevt_a", "mirror": True}
    state = {"skills": [bundle()], "etag": '"1"'}
    requests = []

    class Response(io.BytesIO):
        @property
        def headers(self):
            return {"ETag": state["etag"]}

    def urlopen(request, **kwargs):
        requests.append(request)
        assert request.full_url.endswith("/sync/skills?user_id=alice")
        assert request.get_header("Authorization") == "Bearer tevt_a"
        if request.get_header("If-none-match") == state["etag"]:
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())
        return Response(json.dumps({"status": "ok", "skills": state["skills"]}).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    assert client._pull_from_service(target, cfg)["downloaded"] == 1
    skill_file = target / "test" / "SKILL.md"
    before = skill_file.stat().st_mtime_ns
    assert client._pull_from_service(target, cfg)["downloaded"] == 0
    assert skill_file.stat().st_mtime_ns == before
    state.update(etag='"2"', skills=[bundle(), bundle("new")])
    result = client._pull_from_service(target, cfg)
    assert result["downloaded"] == 1 and result["skipped"] == 1
    assert skill_file.stat().st_mtime_ns == before
    skill_file.write_text("local drift")
    assert client._pull_from_service(target, cfg)["downloaded"] == 1
    assert requests[-1].get_header("If-none-match") is None
    state.update(etag='"3"', skills=[bundle(text="updated", version=2)])
    assert client._pull_from_service(target, cfg)["deleted"] == 1
    assert skill_file.read_text() == "updated"
    assert not (target / "new").exists()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../bad", r"..\bad", "C:/bad"])
def test_bad_bundle_preserves_existing_skill(tmp_path, path):
    client._write_service_bundle(tmp_path, bundle())
    bad = bundle(text="changed")
    bad["files"].append({"path": path, "content_b64": "YQ=="})
    with pytest.raises(ValueError):
        client._write_service_bundle(tmp_path, bad)
    assert (tmp_path / "test" / "SKILL.md").read_text() == "hello"


def test_pull_publish_creates_no_outbox_and_cancels_pending(tmp_path):
    hub = SkillHub(backend="local", endpoint="", local_root=str(tmp_path))
    config = SimpleNamespace(skills_delivery_mode="push")
    calls = []

    async def delivery(*args):
        calls.append(args)
        return {"status": "no_capable_agents", "results": {}}

    service = SkillMutationService.from_hub(hub, config=config, deliverer=delivery)
    old = service.record_committed(action="publish", mutation_id="old", expected={"name": "old", "version": 1}, tenant_ids=[])
    config.skills_delivery_mode = "pull"
    new = service.record_committed(action="publish", mutation_id="new", expected={"name": "new", "version": 1}, tenant_ids=[])
    assert new["status"] == "published" and new["event_id"] == ""
    assert len(list(hub._bucket.iter_objects("skill_sync_outbox/"))) == 1
    result = asyncio.run(service.drain())
    assert result["synced"] == 0 and result["cancelled"] == 1 and not calls
    event = service._read_json(service._event_key(old["event_id"]))
    assert event["status"] == "cancelled"
    assert event["reason"] == "delivery_mode_changed_to_pull"
    assert service.cancel_pending_for_pull() == 0
    with pytest.raises(ValueError, match="delivery_mode_changed_to_pull"):
        service.retry(old["event_id"])
    # During the compatibility window, push with no recipients is also not synced.
    config.skills_delivery_mode = "push"
    event = service.record_committed(action="publish", mutation_id="another", expected={"name": "another"}, tenant_ids=[])
    result = asyncio.run(service.drain())
    assert result["synced"] == 0 and result["available"] == 1
    assert service._read_json(service._event_key(event["event_id"]))["status"] == "available"
