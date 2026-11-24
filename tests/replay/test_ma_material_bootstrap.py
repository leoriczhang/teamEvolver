import hashlib
import io
import json
import urllib.request

import pytest

from team_replay.adapters.ma_materials import bootstrap_environment, bootstrap_source
from team_replay.hooks import ReplayUnsupported


@pytest.mark.parametrize("changed", [False, True])
def test_sandbox_download_checks_actual_bytes_and_separates_credentials(tmp_path, monkeypatch, changed):
    raw = b"frozen bytes"
    material = {"file_id": "source", "mount_path": "nested/input.bin", "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                assert request.full_url == "https://ma.example/api/v3/files/source"
                assert request.get_header("Authorization") == "Bearer test-key"
                data = json.dumps({"id": "source", "status": "active",
                                   "download_url": "https://files.example/input?signature=secret"}).encode()
            else:
                assert request.get_header("Authorization") is None
                data = b"changed data" if changed else raw
            return io.BytesIO(data)

    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    namespace = {"__name__": "bootstrap_test"}
    exec(bootstrap_source("https://ma.example/api/v3", [material]), namespace)
    namespace["MOUNT_ROOT"] = str(tmp_path)
    if changed:
        with pytest.raises(ValueError, match="content mismatch"):
            namespace["main"]()
        assert not list(tmp_path.rglob("*"))
    else:
        namespace["main"]()
        assert (tmp_path / "nested/input.bin").read_bytes() == raw
    assert len(requests) == 2


def test_bootstrap_retains_original_setup_and_enforces_config_size():
    original = {"type": "cloud", "setup_script": "echo original", "env": {"PROFILE": "original"}}
    updated = bootstrap_environment(original, "https://ma.example/api/v3", [])
    assert updated["setup_script"].endswith("echo original")
    assert updated["env"] == original["env"]
    assert original["setup_script"] == "echo original"
    with pytest.raises(ReplayUnsupported, match="config limit"):
        bootstrap_environment({**original, "setup_script": "x" * 65535}, "https://ma.example/api/v3", [])
