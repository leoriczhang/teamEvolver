"""Portable adapter regressions; durable service/API tests use real PostgreSQL."""

import io
import json
import subprocess
import zipfile

import httpx
import pytest

from team_skills.transfer.channels import MarketplaceAdapter, ZipAdapter
from team_skills.transfer.git import GitAdapter
from team_skills.transfer.packages import TransferError, discover, make_zip, read_zip
from tests.support.transfer import bundle, encoded


def test_zip_round_trip_complete_bundle():
    assert ZipAdapter().read(encoded(bundle()))[0].files == bundle().files
    with pytest.raises(TransferError):
        ZipAdapter().read({"zip_b64": "not-base64"})


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "C:/escape", "demo/../../escape"])
def test_archive_rejects_unsafe_paths(path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo/SKILL.md", bundle().files["SKILL.md"])
        archive.writestr(path, b"x")
    with pytest.raises(TransferError):
        read_zip(stream.getvalue())


def test_duplicate_names_missing_description_and_symlink_rejected():
    with pytest.raises(TransferError):
        discover({"a/SKILL.md": bundle().files["SKILL.md"], "b/SKILL.md": bundle().files["SKILL.md"]})
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        entry = zipfile.ZipInfo("demo/link")
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(TransferError):
        read_zip(raw.getvalue())
    with pytest.raises(TransferError):
        discover({"a/SKILL.md": b"---\nname: a\n---\nno description"})


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def remote_repo(tmp_path):
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
    return remote, seed, original


def test_git_revision_subdirectory_branch_and_readonly_reconcile(tmp_path):
    remote, seed, original = remote_repo(tmp_path)
    (seed / "nested/skills/demo/SKILL.md").write_bytes(bundle(body="later").files["SKILL.md"])
    git(seed, "add", ".")
    git(seed, "commit", "-m", "later")
    git(seed, "push", "origin", "main")
    adapter = GitAdapter(allow_local=True)
    options = {"url": str(remote), "branch": "main", "path": "nested/skills", "commit": original}
    assert adapter.read(options)[0].files == bundle().files
    checkpoints = []
    changed = bundle(body="new branch")
    out = adapter.write([changed], {**options, "new_branch": "skills-result", "_checkpoint": checkpoints.append})
    assert git(remote, "show", "skills-result:README.md") == "keep this"
    assert git(remote, "show", "main:nested/skills/demo/SKILL.md").endswith("later")
    assert adapter.read({**options, "branch": "skills-result", "commit": ""})[0].files == changed.files
    assert adapter.reconcile(checkpoints[-2], {}) == out.metadata
    with pytest.raises(TransferError, match="已存在"):
        adapter.write([changed], {**options, "new_branch": "skills-result"})
    with pytest.raises(TransferError):
        GitAdapter().read(options)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_hosted_repository_creation_contract(tmp_path, monkeypatch, provider):
    from team_skills.transfer import git as git_channel

    remote, requests = tmp_path / "new.git", []

    def create(method, url, **kwargs):
        requests.append((method, url, kwargs))
        remote.mkdir()
        git(remote, "init", "--bare")
        return {
            "clone_url": str(remote),
            "html_url": "https://git.test/new",
            "http_url_to_repo": str(remote),
            "web_url": "https://git.test/new",
        }

    monkeypatch.setattr(git_channel, "request_json", create)
    result = GitAdapter(allow_local=True).write(
        [bundle()],
        {
            "mode": "new_repository",
            "provider": provider,
            "repo_name": "new",
            "token": "secret",
            "namespace": "12" if provider == "gitlab" else "example-org",
        },
    )
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


def test_marketplace_download_and_publish_wire_contract(monkeypatch):
    from team_skills.transfer import channels

    calls, real_client = [], httpx.Client

    def respond(request):
        calls.append(request)
        return (
            httpx.Response(200, content=make_zip([bundle()]))
            if request.method == "GET"
            else httpx.Response(200, json={"ok": True})
        )

    monkeypatch.setattr(
        channels.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)
    )
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


@pytest.mark.parametrize("status", [401, 302])
def test_marketplace_errors_and_redirects_do_not_leak_tokens(monkeypatch, status):
    from team_skills.transfer import channels

    real_client, calls = httpx.Client, []

    def remote(request):
        calls.append(request)
        return httpx.Response(status, text="secret", headers={"Location": "https://other.test"})

    monkeypatch.setattr(
        channels.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(remote), **kwargs)
    )
    with pytest.raises(TransferError) as error:
        MarketplaceAdapter().read({"slug": "demo", "token": "secret"})
    assert "secret" not in str(error.value) and len(calls) == 1


def test_git_bounds_and_environment_isolation(tmp_path, monkeypatch):
    from team_skills.transfer import process

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.evil")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!touch /tmp/te-should-not-exist")
    with GitAdapter()._workspace({}) as (repo, run):
        assert run("config", "--get", "alias.evil", check=False).returncode != 0
        monkeypatch.setattr(process, "MAX_OUTPUT_BYTES", 1)
        with pytest.raises(TransferError, match="输出"):
            run("--version")
    monkeypatch.setattr(process, "GIT_TIMEOUT", 0)
    with GitAdapter()._workspace({}) as (_repo, run):
        with pytest.raises(TransferError, match="超时"):
            run("--version")
    monkeypatch.setattr(process, "MAX_GIT_BYTES", 2)
    (tmp_path / "big").write_bytes(b"abc")
    with pytest.raises(TransferError, match="大小"):
        process.check_workspace(tmp_path)
