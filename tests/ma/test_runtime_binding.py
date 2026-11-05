import base64
import io
import random
import re
import subprocess
import tarfile

import pytest

from team_ma.runtime_binding import bind_runtime


def test_mounted_runtime_avoids_large_script_and_checks_bytes_before_bootstrap(tmp_path):
    working = "/workspace/example"
    mount = tmp_path / "skills" / "example"
    mount.mkdir(parents=True)
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": working, "source": "skill_mount", "skill_name": "example",
               "bootstrap": "touch bootstrap-ran"}
    files = {"runtime/code.bin": random.Random(17).randbytes(150000),
             "runtime/start.py": b"print('latest runtime')"}
    for path, data in files.items():
        target = mount / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    desired, tree = bind_runtime({"type": "cloud"}, binding, files)
    _, embedded_tree = bind_runtime({"type": "cloud"}, {**binding, "source": "embedded"}, files)
    assert tree == embedded_tree
    assert len(desired["setup_script"].encode()) < 16384
    script = desired["setup_script"].replace("/mnt/skills/example", str(mount)).replace(
        working, str(tmp_path / "good"))
    subprocess.run(["bash", "-c", script], check=True)
    assert (tmp_path / "good/bootstrap-ran").exists()
    for path, data in files.items():
        assert (tmp_path / "good" / path).read_bytes() == data
    assert (tmp_path / "good/.team-evolver-runtime-sha256").read_text() == tree

    (mount / "runtime/start.py").write_bytes(b"wrong release")
    bad = subprocess.run(["bash", "-c", script.replace(str(tmp_path / "good"), str(tmp_path / "bad"))],
                         capture_output=True)
    assert bad.returncode != 0
    assert b"Mounted runtime hash mismatch" in bad.stderr
    assert not (tmp_path / "bad/bootstrap-ran").exists()
    assert not (tmp_path / "bad/runtime/code.bin").exists()


@pytest.mark.parametrize("name", ["", "../other", "nested/skill", ".."])
def test_mounted_runtime_rejects_invalid_skill_roots(name):
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": "/workspace/example", "source": "skill_mount", "skill_name": name}
    with pytest.raises(ValueError):
        bind_runtime({"type": "cloud"}, binding, {"runtime/start.py": b"pass"})


def test_runtime_uses_exact_bundle_bytes_and_preserves_environment_fields():
    config = {"type": "cloud", "packages": {"apt": ["ffmpeg"]}, "env": {"PROFILE": "example"},
              "setup_script": "echo original"}
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": "/workspace/example", "bootstrap": "bash runtime/bootstrap.sh"}
    files = {"runtime/bootstrap.sh": b"echo changed", "runtime/render.mjs": b"export const version=2;"}
    desired, tree = bind_runtime(config, binding, files)
    repeated, same_tree = bind_runtime(config, binding, files)
    assert desired == repeated and tree == same_tree
    assert desired["packages"] == config["packages"]
    assert desired["env"] == config["env"]
    encoded = re.search(r"printf '%s' '([^']+)'", desired["setup_script"]).group(1)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as archive:
        assert {item.name: archive.extractfile(item).read() for item in archive.getmembers()} == files
    assert config["setup_script"] == "echo original"


def test_runtime_rejects_traversal_and_setup_overflow():
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "../escape/"}],
               "working_directory": "/workspace/example"}
    with pytest.raises(ValueError, match="Unsafe"):
        bind_runtime({"type": "cloud"}, binding, {"runtime/a": b"data"})
    binding["paths"][0]["workspace_prefix"] = "runtime/"
    with pytest.raises(ValueError, match="256 KiB"):
        bind_runtime({"type": "cloud", "setup_script": "x" * 262144}, binding, {"runtime/a": b"data"})


def test_prerequisites_run_before_source_bootstrap_and_do_not_change_bundle_hash():
    config = {"type": "cloud", "setup_script": "bash existing/bootstrap.sh"}
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": "/workspace/example", "pre_setup": "install-fixed-node",
               "bootstrap": "bash runtime/bootstrap.sh"}
    files = {"runtime/bootstrap.sh": b"node --version"}
    desired, tree = bind_runtime(config, binding, files)
    assert desired["setup_script"].startswith("install-fixed-node\nbash existing/bootstrap.sh")
    _, original_tree = bind_runtime(config, {k: v for k, v in binding.items() if k != "pre_setup"}, files)
    assert original_tree == tree
    replaced, replaced_tree = bind_runtime(config, {**binding, "replace_setup_script": True}, files)
    assert "existing/bootstrap.sh" not in replaced["setup_script"]
    assert "bash runtime/bootstrap.sh" in replaced["setup_script"]
    assert replaced_tree == tree


@pytest.mark.parametrize("compression", ["gzip", "xz"])
def test_runtime_compression_extracts_exact_bytes_and_preserves_tree(compression, tmp_path):
    working = "/workspace/example"
    config = {"type": "cloud"}
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": working, "archive_compression": compression}
    files = {"runtime/a.py": b"print('runtime')\n", "runtime/nested/data": bytes(range(256))}
    desired, tree = bind_runtime(config, binding, files)
    repeated, repeated_tree = bind_runtime(config, binding, dict(reversed(list(files.items()))))
    default, default_tree = bind_runtime(config, {k: v for k, v in binding.items()
                                                 if k != "archive_compression"}, files)
    assert desired == repeated and tree == repeated_tree == default_tree
    if compression == "gzip":
        assert desired == default
    encoded = re.search(r"printf '%s' '([^']+)'", desired["setup_script"]).group(1)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:" + (
            "xz" if compression == "xz" else "gz")) as archive:
        assert {item.name: archive.extractfile(item).read() for item in archive.getmembers()} == files
    # Execute the actual generated decoder; no external xz executable is needed.
    subprocess.run(["bash", "-c", desired["setup_script"].replace(working, str(tmp_path))], check=True)
    for name, content in files.items():
        assert (tmp_path / name).read_bytes() == content
    assert (tmp_path / ".team-evolver-runtime-sha256").read_text() == tree


@pytest.mark.parametrize("compression", ["zip", "", None, 1, {}, []])
def test_runtime_rejects_unsupported_archive_compression(compression):
    binding = {"paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "working_directory": "/workspace/example", "archive_compression": compression}
    with pytest.raises(ValueError, match="archive_compression"):
        bind_runtime({"type": "cloud"}, binding, {"runtime/a": b"data"})
