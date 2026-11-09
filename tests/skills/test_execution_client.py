"""Agent cache acceptance: complete releases, atomic activation and stable task pins."""
from __future__ import annotations

import base64
import copy
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from team_skills.execution import client
from team_skills.execution.client import ExecutionReleaseCache
from team_skills.execution.contracts import composition_hash, plan_for, text_hash, validate_profile
from team_skills.library.bundle import bundle_tree_sha256
from tests.support.execution import profile_raw
from tests.support.skills import _skill_md


def _release(label="old"):
    profile = validate_profile(profile_raw())
    profile["components"][0].update(content=label, content_hash=text_hash(label))
    bundles = {name: {"SKILL.md": _skill_md(name, body=label), "references/input.bin": b"\x00\xffsource"}
               for name in profile["affected_skill_ids"]}
    dependencies = [{"name": name, "version": 1, "tree_sha256": bundle_tree_sha256(bundle)}
                    for name, bundle in bundles.items()]
    document = {"execution_plan": plan_for(profile, profile["components"]),
                "skill_dependencies": dependencies, "origin": "test"}
    return {
        "release_id": "release-" + label, "document": document, "composition_hash": composition_hash(document),
        "skills": [{**dependency, "files": [
            {"path": path, "content_b64": base64.b64encode(data).decode("ascii")}
            for path, data in bundles[dependency["name"]].items()
        ]} for dependency in dependencies],
    }


def _rehash(release):
    release["composition_hash"] = composition_hash(release["document"])


def _no_temporary_files(root):
    assert not list(root.glob(".execution-stage-*"))
    assert not list(root.glob(".active-*"))


def test_installs_the_complete_combo_and_checks_every_dependency(tmp_path):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    release = _release()
    result = cache.install(release)
    assert result == cache.active()
    assert result["release_id"] == release["release_id"]
    assert result["document"] == release["document"]
    assert (Path(result["directory"]) / "skills/helper/references/input.bin").read_bytes() == b"\x00\xffsource"
    assert (Path(result["directory"]) / "skills/demo/SKILL.md").read_bytes() == _skill_md("demo", body="old")
    _no_temporary_files(cache.root)


@pytest.mark.parametrize("fault", ["composition", "component", "missing_skill", "extra_skill", "duplicate_skill",
                                    "dependency_version", "boolean_version", "duplicate_dependency", "dependency_hash",
                                    "base64", "base64_type", "duplicate_file", "missing_entrypoint", "wire_hash"])
def test_rejects_invalid_releases_before_writing_or_activating(tmp_path, fault):
    release = _release()
    if fault == "composition":
        release["composition_hash"] = "0" * 64
    elif fault == "component":
        release["document"]["execution_plan"]["components"][0]["content"] = "unhashed change"
    elif fault == "missing_skill":
        release["skills"].pop()
    elif fault == "extra_skill":
        release["skills"].append(copy.deepcopy(release["skills"][0]))
    elif fault == "duplicate_skill":
        release["skills"][1] = copy.deepcopy(release["skills"][0])
    elif fault == "dependency_version":
        release["document"]["skill_dependencies"][0]["version"] = 2
    elif fault == "boolean_version":
        release["skills"][0]["version"] = True
    elif fault == "duplicate_dependency":
        release["document"]["skill_dependencies"].append(copy.deepcopy(release["document"]["skill_dependencies"][0]))
    elif fault == "dependency_hash":
        release["document"]["skill_dependencies"][0]["tree_sha256"] = "0" * 64
    elif fault == "base64":
        release["skills"][0]["files"][0]["content_b64"] = "not valid!"
    elif fault == "base64_type":
        release["skills"][0]["files"][0]["content_b64"] = 123
    elif fault == "duplicate_file":
        release["skills"][0]["files"].append(copy.deepcopy(release["skills"][0]["files"][0]))
    elif fault == "missing_entrypoint":
        release["skills"][0]["files"] = release["skills"][0]["files"][1:]
    elif fault == "wire_hash":
        release["skills"][0]["tree_sha256"] = "0" * 64
    if fault != "composition":
        _rehash(release)
    cache = ExecutionReleaseCache(tmp_path / "cache")
    with pytest.raises(ValueError):
        cache.install(release)
    assert not cache.root.exists()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "C:escape", "refs\\escape",
                                 "refs//escape", "refs/./escape", "refs/../escape", ".DS_Store",
                                 "__pycache__/escape.py", "SKILL.md/", ".", "bad\x00path"])
def test_rejects_unsafe_or_noncanonical_paths_without_touching_the_host(tmp_path, path):
    release = _release()
    release["skills"][0]["files"].append({"path": path, "content_b64": "ZXNjYXBl"})
    cache = ExecutionReleaseCache(tmp_path / "cache")
    with pytest.raises(ValueError, match="path"):
        cache.install(release)
    assert not cache.root.exists()
    assert not (tmp_path / "escape").exists()


def test_rejects_file_directory_collisions(tmp_path):
    release = _release()
    release["skills"][0]["files"].extend([
        {"path": "refs", "content_b64": "YQ=="}, {"path": "refs/a.md", "content_b64": "YQ=="},
    ])
    with pytest.raises(ValueError, match="directory"):
        ExecutionReleaseCache(tmp_path / "cache").install(release)


@pytest.mark.parametrize("phase", ["bundle_write", "directory_swap", "pointer_sync", "pointer_swap"])
def test_install_failure_preserves_the_old_active_combo_and_cleans_temporary_files(tmp_path, monkeypatch, phase):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    old = cache.install(_release())
    original_replace, original_write = client.os.replace, client.write_skill_bundle

    def replace(source, destination):
        if phase == "pointer_swap" and Path(destination) == cache.root / "active":
            raise OSError("injected active pointer failure")
        if phase == "directory_swap" and Path(destination).name == _release("new")["composition_hash"]:
            raise OSError("injected staging promotion failure")
        return original_replace(source, destination)

    def write(*args, **kwargs):
        original_write(*args, **kwargs)
        raise OSError("injected partial bundle write")

    with monkeypatch.context() as patch:
        patch.setattr(client.os, "replace", replace)
        if phase == "bundle_write":
            patch.setattr(client, "write_skill_bundle", write)
        if phase == "pointer_sync":
            patch.setattr(client.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("injected sync failure")))
        with pytest.raises(OSError, match="injected"):
            cache.install(_release("new"))
    assert cache.active() == old
    assert (Path(old["directory"]) / "skills/demo/SKILL.md").read_bytes() == _skill_md("demo", body="old")
    _no_temporary_files(cache.root)
    assert cache.install(_release("new"))["release_id"] == "release-new"


def test_checks_staged_bytes_before_activating(tmp_path, monkeypatch):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    old = cache.install(_release())
    original_write = client.write_skill_bundle

    def contaminated_write(root, bundle, **kwargs):
        original_write(root, bundle, **kwargs)
        (Path(root) / "unexpected.py").write_text("untracked")

    monkeypatch.setattr(client, "write_skill_bundle", contaminated_write)
    with pytest.raises(ValueError, match="modified"):
        cache.install(_release("new"))
    assert cache.active() == old
    _no_temporary_files(cache.root)


def test_idempotent_install_never_rewrites_an_existing_release(tmp_path, monkeypatch):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    release = _release()
    pinned = cache.install(release)
    original = client.os.replace

    def replace(source, destination):
        assert Path(destination) != Path(pinned["directory"])
        return original(source, destination)

    monkeypatch.setattr(client.os, "replace", replace)
    assert cache.install(release) == pinned
    _no_temporary_files(cache.root)


@pytest.mark.parametrize("change", ["bytes", "extra", "missing", "symlink"])
def test_active_and_reinstall_detect_modified_cached_skill_inventory(tmp_path, change):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    release = _release()
    pinned = cache.install(release)
    skill = Path(pinned["directory"]) / "skills/demo"
    target = skill / "SKILL.md"
    if change == "bytes":
        target.write_text("modified")
    elif change == "extra":
        (skill / "unexpected.sh").write_text("untracked")
    elif change == "missing":
        target.unlink()
    else:
        external = tmp_path / "external"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    with pytest.raises(ValueError):
        cache.active()
    with pytest.raises(ValueError):
        cache.install(release)
    _no_temporary_files(cache.root)


def test_target_symlink_cannot_redirect_installation_outside_cache(tmp_path):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    old = cache.install(_release())
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("unchanged")
    (cache.root / _release("new")["composition_hash"]).symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        cache.install(_release("new"))
    assert cache.active() == old
    assert sentinel.read_text() == "unchanged"
    assert list(external.iterdir()) == [sentinel]


@pytest.mark.parametrize("pointer", ["../outside", "0" * 63, "g" * 64, "0" * 64 + "\n"])
def test_active_pointer_is_not_a_path_or_arbitrary_file_reference(tmp_path, pointer):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    cache.root.mkdir()
    (cache.root / "active").write_text(pointer)
    with pytest.raises(ValueError, match="hash"):
        cache.active()


def test_pinned_task_and_caller_inputs_do_not_change_when_a_new_combo_activates(tmp_path):
    cache = ExecutionReleaseCache(tmp_path / "cache")
    wire = _release()
    old = cache.install(wire)
    wire["document"]["execution_plan"]["components"][0]["content"] = "caller mutation"
    assert old["document"]["execution_plan"]["components"][0]["content"] == "old"
    new = cache.install(_release("new"))
    assert cache.active() == new
    assert new["directory"] != old["directory"]
    assert old["release_id"] == "release-old"
    assert (Path(old["directory"]) / "skills/demo/SKILL.md").read_bytes() == _skill_md("demo", body="old")
    new["document"]["execution_plan"]["components"][0]["content"] = "local mutation"
    assert cache.active()["document"]["execution_plan"]["components"][0]["content"] == "new"


def test_concurrent_instances_install_idempotently_and_return_their_own_task_pin(tmp_path):
    root = tmp_path / "cache"
    releases = [_release("one"), _release("two")] * 6
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(lambda release: ExecutionReleaseCache(root).install(release), releases))
    assert [result["release_id"] for result in results] == [release["release_id"] for release in releases]
    assert ExecutionReleaseCache(root).active()["release_id"] in {"release-one", "release-two"}
    assert len([entry for entry in root.iterdir() if entry.is_dir()]) == 2
    _no_temporary_files(root)


def _process_install(root, release, barrier, results):
    try:
        barrier.wait(timeout=10)
        pinned = ExecutionReleaseCache(root).install(release)
        results.put(("ok", pinned["release_id"]))
    except Exception as exc:
        results.put(("error", repr(exc)))


@pytest.mark.skipif(os.name != "posix", reason="reference cache uses a POSIX local installation lock")
def test_separate_agent_processes_can_install_the_same_release(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_process_install, args=(str(tmp_path / "cache"), _release(), barrier, results))
                 for _ in range(2)]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        outcomes = [results.get(timeout=15) for _ in processes]
        assert outcomes == [("ok", "release-old"), ("ok", "release-old")]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert ExecutionReleaseCache(tmp_path / "cache").active()["release_id"] == "release-old"
        _no_temporary_files(tmp_path / "cache")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)
        results.close()
