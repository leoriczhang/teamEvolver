"""Transfer contracts: real ZIP/Git round trips, versions, isolation and auth."""
from __future__ import annotations

import base64
import io
import json
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_skills.library.mutations import SkillMutationService
from team_skills.transfer.channels import MarketplaceAdapter
from team_skills.transfer.git import GitAdapter
from team_skills.transfer.packages import TransferError, discover, make_zip, package, read_zip
from team_skills.transfer.service import SkillTransferService
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy.skills_admin import SkillsAdminMixin
from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant


def bundle(name="demo", body="one"):
    return package(name, {"SKILL.md": f"---\nname: {name}\ndescription: Demo\n---\n{body}\n".encode(),
                          "scripts/run.py": b"print('hello')\n", "assets/data.bin": bytes(range(256))})


def encoded(*items):
    return {"zip_b64": base64.b64encode(make_zip(items)).decode()}


def config(tmp_path):
    return TeamEvolverConfig(skills_dir=str(tmp_path / "working"), sharing_enabled=True,
                             sharing_skill_backend="local", sharing_skill_local_root=str(tmp_path / "store"),
                             sharing_skill_mirror_enabled=False, skills_delivery_mode="pull")


def service(tmp_path, tenant="default", adapters=None):
    cfg = config(tmp_path)
    return SkillTransferService(str(tmp_path / "working" / tenant),
                                SkillMutationService.from_config(cfg, tenant_id=tenant), adapters)


def test_zip_round_trip_complete_bundle_and_version_noop(tmp_path):
    svc = service(tmp_path)
    assert svc.import_skills("zip", encoded(bundle()))["imported"][0]["version"] == 1
    repeat = svc.import_skills("zip", encoded(bundle()))["imported"][0]
    assert repeat["version"] == 1 and repeat["status"] == "unchanged"
    assert svc.import_skills("zip", encoded(bundle(body="two")))["imported"][0]["version"] == 2
    # Export the versioned source even if a stale working copy exists.
    Path(svc.skills_dir, "demo", "SKILL.md").write_text("stale")
    exported = svc.export_skills("zip", ["demo"], {})
    restored = discover(read_zip(exported.content))[0]
    assert restored.files == bundle(body="two").files
    assert svc.hub._read_version_bundle("demo", 1) == bundle().files
    assert not list((tmp_path / "store").glob("skill_sync_outbox/*"))


def test_conflicts_and_same_name_across_tenants(tmp_path):
    first, second = service(tmp_path, "a"), service(tmp_path, "b")
    first.import_skills("zip", encoded(bundle()))
    assert not second.list_skills()
    result = first.import_skills("zip", encoded(bundle(body="two")), conflict="skip")
    assert result["skipped"][0]["name"] == "demo"
    with pytest.raises(TransferError):
        first.import_skills("zip", encoded(bundle("new"), bundle()), conflict="error")
    assert [s["name"] for s in first.list_skills()] == ["demo"]
    second.import_skills("zip", encoded(bundle(body="tenant b")))
    assert first.hub._read_version_bundle("demo", 1) != second.hub._read_version_bundle("demo", 1)


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "C:/escape", "demo/../../escape"])
def test_archive_rejects_unsafe_paths_before_write(tmp_path, path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo/SKILL.md", bundle().files["SKILL.md"])
        archive.writestr(path, b"x")
    with pytest.raises(TransferError):
        service(tmp_path).import_skills("zip", {"zip_b64": base64.b64encode(stream.getvalue()).decode()})
    assert not (tmp_path / "working").exists()


def test_archive_duplicate_names_invalid_skill_and_symlink_are_rejected(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(TransferError):
        discover({"a/SKILL.md": bundle().files["SKILL.md"], "b/SKILL.md": bundle().files["SKILL.md"]})
    with pytest.raises(TransferError):
        svc.import_skills("zip", {"zip_b64": "not-base64"})
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        entry = zipfile.ZipInfo("demo/link")
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(TransferError):
        read_zip(raw.getvalue())
    with pytest.raises(TransferError):
        discover({"a/SKILL.md": b"---\nname: a\n---\nno description"})


def test_storage_failure_does_not_replace_working_bundle(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.import_skills("zip", encoded(bundle()))
    def fail(command):
        raise RuntimeError("token-must-not-leak")
    monkeypatch.setattr(svc.mutations, "execute", fail)
    result = svc.import_skills("zip", encoded(bundle(body="replacement")))
    assert result["errors"] and "token-must-not-leak" not in json.dumps(result)
    assert Path(svc.skills_dir, "demo", "SKILL.md").read_bytes() == bundle().files["SKILL.md"]


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                          cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_git_revision_subdirectory_and_new_branch_preserve_other_content(tmp_path):
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--bare", str(remote))
    git(seed, "init", "-b", "main")
    skill = seed / "nested" / "skills" / "demo"
    skill.mkdir(parents=True)
    for path, data in bundle().files.items():
        target = skill / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (seed / "README.md").write_text("keep this")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "first")
    original = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")
    adapter = GitAdapter(allow_local=True)
    options = {"url": str(remote), "branch": "main", "path": "nested/skills", "commit": original}
    assert adapter.read(options)[0].files == bundle().files
    changed = bundle(body="new branch")
    out = adapter.write([changed], {**options, "new_branch": "skills-result"})
    assert out.metadata["branch"] == "skills-result"
    assert git(remote, "show", "main:README.md") == "keep this"
    assert git(remote, "show", "skills-result:README.md") == "keep this"
    assert git(remote, "show", "main:nested/skills/demo/SKILL.md").endswith("one")
    assert adapter.read({**options, "branch": "skills-result", "commit": ""})[0].files == changed.files
    with pytest.raises(TransferError, match="已存在"):
        adapter.write([changed], {**options, "new_branch": "skills-result"})
    with pytest.raises(TransferError):
        GitAdapter().read(options)


def test_git_new_repository_creates_then_pushes(tmp_path, monkeypatch):
    remote = tmp_path / "created.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    adapter = GitAdapter(allow_local=True)
    monkeypatch.setattr(adapter, "_create_repository", lambda options: (str(remote), "https://git.test/new"))
    result = adapter.write([bundle()], {"mode": "new_repository"})
    assert result.metadata["branch"] == "main"
    assert git(remote, "show", "main:skills/demo/SKILL.md").endswith("one")


def test_marketplace_download_and_publish_wire_contract(monkeypatch):
    from team_skills.transfer import channels
    calls = []
    real_client = httpx.Client
    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=make_zip([bundle()]))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(channels.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    adapter = MarketplaceAdapter()
    assert adapter.read({"slug": "demo", "version": "1.0.0"})[0].files == bundle().files
    assert calls[0].url.params["version"] == "1.0.0"
    result = adapter.write([bundle()], {"token": "secret", "version": "1.0.1"})
    assert result.metadata["version"] == "1.0.1"
    assert calls[1].headers["authorization"] == "Bearer secret"
    assert b'name="payload"' in calls[1].content and b'name="files[]"' in calls[1].content
    assert b'filename="assets/data.bin"' in calls[1].content
    adapter.write([bundle()], {"provider": "http", "upload_url": "https://market.test/upload"})
    assert b'filename="skills.zip"' in calls[2].content


def test_marketplace_errors_do_not_echo_tokens(monkeypatch):
    from team_skills.transfer import channels
    real_client = httpx.Client
    monkeypatch.setattr(channels.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="secret")), **kwargs))
    with pytest.raises(TransferError, match="认证失败") as error:
        MarketplaceAdapter().read({"slug": "demo", "token": "secret"})
    assert "secret" not in str(error.value)


def test_http_routes_auth_legacy_and_tenant_scope(tmp_path, monkeypatch):
    from teamEvolver.proxy import routes
    cfg = config(tmp_path)
    owner = type("Owner", (SkillsAdminMixin,), {} )()
    owner.config, owner.skill_manager = cfg, None
    monkeypatch.setattr(routes, "_tenant_effective_config", lambda owner: cfg)
    app = FastAPI()
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.console_user = {"role": request.headers.get("role", "user")}
        token = set_current_tenant(TenantContext(tenant_id=request.headers.get("tenant", "default")))
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token)
    owner._register_skills_admin_routes(app)
    with TestClient(app) as client:
        denied = client.post("/api/skills/import", json={"channel": "zip", "options": encoded(bundle())})
        assert denied.status_code == 403
        headers = {"role": "admin", "tenant": "alpha"}
        result = client.post("/api/skills/import-zip", json=encoded(bundle()), headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["version"] == 1
        assert client.get("/api/skills/transfer/channels", headers=headers).status_code == 200
        assert client.get("/api/skills/transfer/skills", headers={**headers, "tenant": "beta"}).json()["skills"] == []
        result = client.post("/api/skills/export", json={"channel": "zip", "names": ["demo"]}, headers=headers)
        assert result.status_code == 200 and "application/zip" in result.headers["content-type"]
        assert discover(read_zip(result.content))[0].files == bundle().files
        # Single compatibility route rejects a batch before any write.
        result = client.post("/api/skills/import-zip", json=encoded(bundle("one"), bundle("two")), headers=headers)
        assert result.status_code == 400
        assert len(client.get("/api/skills/transfer/skills", headers=headers).json()["skills"]) == 1


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_hosted_repository_creation_contract(tmp_path, monkeypatch, provider):
    from team_skills.transfer import git as git_channel
    remote = tmp_path / "new.git"
    requests = []

    def create(method, url, **kwargs):
        requests.append((method, url, kwargs))
        remote.mkdir()
        git(remote, "init", "--bare")
        return {"clone_url": str(remote), "html_url": "https://git.test/new",
                "http_url_to_repo": str(remote), "web_url": "https://git.test/new"}

    monkeypatch.setattr(git_channel, "request_json", create)
    result = GitAdapter(allow_local=True).write([bundle()], {
        "mode": "new_repository", "provider": provider, "repo_name": "new", "token": "secret",
        "namespace": "12" if provider == "gitlab" else "example-org",
    })
    assert requests[0][0] == "POST"
    if provider == "github":
        assert requests[0][1] == "https://api.github.com/orgs/example-org/repos"
        assert requests[0][2]["json"]["private"] is True
        assert requests[0][2]["headers"]["Authorization"] == "Bearer secret"
    else:
        assert requests[0][1] == "https://gitlab.com/api/v4/projects"
        assert requests[0][2]["json"]["namespace_id"] == 12
        assert requests[0][2]["json"]["visibility"] == "private"
    assert "secret" not in json.dumps(result.metadata)
    assert git(remote, "show", "main:skills/demo/SKILL.md").endswith("one")
