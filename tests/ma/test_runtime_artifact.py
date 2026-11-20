import hashlib
import io
import json
import subprocess
import tarfile
import urllib.error

import pytest

from team_ma import runtime_artifact as installer
from team_ma.runtime_binding import bind_runtime

MAPPINGS = [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"},
            {"bundle_prefix": "config/", "workspace_prefix": "settings/"}]


def artifact(files=None, extra=None):
    files = files or {"runtime/a.py": b"print('current')", "config/policy.json": b"{}"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        if extra:
            archive.addfile(extra)
    data = buffer.getvalue()
    descriptor = {"base_url": "https://viking.example/openviking",
                  "uri": "viking://user/example/resources/runtime-pinned/runtime.tar.xz",
                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                  "files": [{"path": p, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                            for p, b in sorted(files.items())]}
    return descriptor, data, files


def test_generated_bootstrap_reuses_verified_cache_without_credentials(tmp_path):
    descriptor, data, files = artifact()
    root = tmp_path / "workspace"
    cache = root / ".team-evolver-runtime-cache"
    cache.mkdir(parents=True)
    (cache / (descriptor["sha256"] + ".tar.xz")).write_bytes(data)
    binding = {"source": "openviking", "artifact": descriptor, "paths": MAPPINGS,
               "working_directory": "/workspace/example", "bootstrap": "touch bootstrap-ran"}
    config, tree = bind_runtime({"type": "cloud"}, binding, {"SKILL.md": b"slim"})
    assert tree == installer.artifact_tree(installer.mapped_files(descriptor, MAPPINGS))
    subprocess.run(["bash", "-c", config["setup_script"].replace("/workspace/example", str(root))], check=True)
    assert (root / "runtime/a.py").read_bytes() == files["runtime/a.py"]
    assert (root / "settings/policy.json").read_bytes() == files["config/policy.json"]
    assert (root / "bootstrap-ran").exists()
    assert json.loads((root / ".team-evolver-runtime-artifact.json").read_text())["cache_hit"] is True
    assert (root / ".team-evolver-runtime-sha256").read_text() == tree


def test_download_then_cache_and_recover_corrupt_cache(tmp_path, monkeypatch):
    descriptor, data, _ = artifact()
    calls = []

    def download(value, target):
        calls.append(value["uri"])
        target.write_bytes(data)

    monkeypatch.setattr(installer, "download_archive", download)
    assert installer.install_artifact(descriptor, MAPPINGS, tmp_path)["cache_hit"] is False
    assert installer.install_artifact(descriptor, MAPPINGS, tmp_path)["cache_hit"] is True
    cached = tmp_path / ".team-evolver-runtime-cache" / (descriptor["sha256"] + ".tar.xz")
    cached.write_bytes(b"corrupt")
    assert installer.install_artifact(descriptor, MAPPINGS, tmp_path)["cache_hit"] is False
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["archive_hash", "file_hash", "missing", "symlink", "traversal"])
def test_bad_artifact_cannot_install_any_runtime_files(tmp_path, monkeypatch, failure):
    extra = None
    if failure in {"symlink", "traversal"}:
        extra = tarfile.TarInfo("../escape" if failure == "traversal" else "runtime/link")
        extra.type, extra.linkname = tarfile.SYMTYPE, "/etc/passwd"
    descriptor, data, _ = artifact(extra=extra)
    if failure == "archive_hash":
        descriptor["sha256"] = "0" * 64
    elif failure == "file_hash":
        descriptor["files"][0]["sha256"] = "0" * 64
    elif failure == "missing":
        descriptor["files"].append({"path": "runtime/missing", "bytes": 0,
                                    "sha256": hashlib.sha256(b"").hexdigest()})
    monkeypatch.setattr(installer, "download_archive", lambda value, target: target.write_bytes(data))
    with pytest.raises(ValueError):
        installer.install_artifact(descriptor, MAPPINGS, tmp_path)
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / ".team-evolver-runtime-artifact.json").exists()


def test_redirects_are_rejected_and_never_reveal_credentials(tmp_path, monkeypatch):
    descriptor, _, _ = artifact()
    monkeypatch.setenv("OPENVIKING_API_KEY", "private-test-key")
    assert installer.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer private-test-key"
            raise urllib.error.HTTPError(request.full_url, 302, "private-test-key", {}, None)

    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="no redirects") as error:
        installer.download_archive(descriptor, tmp_path / "archive")
    assert "private-test-key" not in str(error.value)


@pytest.mark.parametrize("change", [
    {"base_url": "http://viking.example"}, {"base_url": "https://secret@viking.example"},
    {"uri": "viking://user/a/resources/../escape"}, {"sha256": "wrong"}, {"bytes": 100_000_000},
])
def test_invalid_pins_are_rejected_before_network(change):
    descriptor, _, _ = artifact()
    with pytest.raises(ValueError):
        installer.mapped_files({**descriptor, **change}, MAPPINGS)
