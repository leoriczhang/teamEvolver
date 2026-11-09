"""Agent-side immutable release cache with a process-safe atomic active pointer.

This is a rebuildable execution cache, never a server authority or fallback.
Agents pin active() once per task. New installs retain all prior directories;
callers must not write into a pinned release. Activation is atomic visibility,
not a claim of durable recovery from machine or filesystem failure.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from team_replay.execution_plan import normalize_execution_plan
from team_skills.library.bundle import bundle_tree_sha256, is_ignored_bundle_rel_path, write_skill_bundle
from team_skills.library.editor import validate_skill_name
from team_skills.library.frontmatter import validate_skill_md

from .contracts import composition_hash

_SHA = re.compile(r"[0-9a-f]{64}")


def _hash(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("invalid execution content hash")
    return value


def _name(value):
    if not isinstance(value, str) or validate_skill_name(value) != value:
        raise ValueError("execution Skill name must be canonical")
    return value


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("unsafe execution Skill path")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or path.as_posix() != value
            or not path.parts or any(part in {".", ".."} for part in path.parts)
            or is_ignored_bundle_rel_path(value)):
        raise ValueError("unsafe or noncanonical execution Skill path")
    return value


def _document(document, sha):
    if not isinstance(document, dict) or composition_hash(document) != _hash(sha):
        raise ValueError("execution composition hash mismatch")
    if normalize_execution_plan(document.get("execution_plan")) is None:
        raise ValueError("execution release requires an execution plan")
    raw = document.get("skill_dependencies")
    if not isinstance(raw, list):
        raise ValueError("execution Skill dependencies must be a list")
    dependencies = {}
    for dependency in raw:
        if not isinstance(dependency, dict) or set(dependency) != {"name", "version", "tree_sha256"}:
            raise ValueError("invalid execution Skill dependency")
        name = _name(dependency["name"])
        if name in dependencies or type(dependency["version"]) is not int or dependency["version"] < 1:
            raise ValueError("duplicate execution Skill dependency or invalid version")
        _hash(dependency["tree_sha256"])
        dependencies[name] = dependency
    return dependencies


def _bundles(release, dependencies):
    supplied = release.get("skills")
    if not isinstance(supplied, list) or len(supplied) != len(dependencies):
        raise ValueError("incomplete execution Skill dependencies")
    bundles = {}
    for skill in supplied:
        if not isinstance(skill, dict):
            raise ValueError("execution Skill must be an object")
        name = _name(skill.get("name"))
        expected = dependencies.get(name)
        if (not expected or name in bundles or type(skill.get("version")) is not int
                or skill["version"] != expected["version"]):
            raise ValueError("unexpected execution Skill version")
        files = skill.get("files")
        if not isinstance(files, list):
            raise ValueError("execution Skill files must be a list")
        bundle = {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("execution Skill file must be an object")
            path = _path(item.get("path"))
            if path in bundle:
                raise ValueError("duplicate execution Skill file")
            raw = item.get("content_b64")
            try:
                if not isinstance(raw, str):
                    raise ValueError("base64 content must be text")
                bundle[path] = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("invalid execution Skill base64") from exc
        for path in bundle:
            if any(str(parent) in bundle for parent in PurePosixPath(path).parents if str(parent) != "."):
                raise ValueError("execution Skill file conflicts with a directory")
        if "SKILL.md" not in bundle:
            raise ValueError("execution Skill requires SKILL.md")
        validate_skill_md(bundle["SKILL.md"], expected_name=name)
        sha = bundle_tree_sha256(bundle)
        if sha != expected["tree_sha256"] or skill.get("tree_sha256", sha) != sha:
            raise ValueError("execution Skill hash mismatch")
        bundles[name] = bundle
    return bundles


def _manifest(release):
    # Own a JSON snapshot so caller mutation cannot change already checked bytes.
    try:
        value = json.loads(json.dumps(release, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("execution release must contain JSON values") from exc
    if not isinstance(value, dict) or not isinstance(value.get("release_id"), str) or not value["release_id"]:
        raise ValueError("execution release_id is required")
    document, sha = value.get("document"), value.get("composition_hash")
    dependencies = _document(document, sha)
    manifest = {"release_id": value["release_id"], "composition_hash": sha, "document": document}
    return manifest, _bundles(value, dependencies)


def _verify_directory(directory, manifest, bundles=None):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("immutable execution cache is not a directory")
    files = {}
    for path in directory.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("immutable execution cache contains an unsafe file")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path
    if "manifest.json" not in files or json.loads(files["manifest.json"].read_text(encoding="utf-8")) != manifest:
        raise ValueError("immutable execution cache has conflicting manifest")
    dependencies = _document(manifest["document"], manifest["composition_hash"])
    expected = {"manifest.json"}
    for name, dependency in dependencies.items():
        prefix = f"skills/{name}/"
        disk = {}
        for relative, path in files.items():
            if relative.startswith(prefix):
                inner = _path(relative[len(prefix):])
                disk[inner] = path.read_bytes()
                expected.add(relative)
        if ("SKILL.md" not in disk or bundle_tree_sha256(disk) != dependency["tree_sha256"]
                or (bundles is not None and disk != bundles[name])):
            raise ValueError("immutable execution cache was modified")
    if set(files) != expected:
        raise ValueError("immutable execution cache contains unexpected files")


class ExecutionReleaseCache:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def _install_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("execution cache root cannot be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.root / ".execution-install.lock", flags, 0o600)
        try:
            # A local Agent cache lock, shared across installers/processes only.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def install(self, release):
        manifest, bundles = _manifest(release)
        sha = manifest["composition_hash"]
        with self._install_lock():
            stage = Path(tempfile.mkdtemp(prefix=".execution-stage-", dir=self.root))
            target = self.root / sha
            try:
                for name, bundle in bundles.items():
                    write_skill_bundle(stage / "skills" / name, bundle, clean=True)
                (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                _verify_directory(stage, manifest, bundles)
                if target.exists() or target.is_symlink():
                    _verify_directory(target, manifest, bundles)
                else:
                    os.replace(stage, target)
                pointer_fd, pointer_name = tempfile.mkstemp(prefix=".active-", dir=self.root)
                pointer = Path(pointer_name)
                try:
                    with os.fdopen(pointer_fd, "w", encoding="ascii") as output:
                        output.write(sha)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(pointer, self.root / "active")
                finally:
                    pointer.unlink(missing_ok=True)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            # Return this install's pin, not another racing activation's pointer.
            return {**manifest, "directory": str(target)}

    def active(self):
        pointer = self.root / "active"
        if self.root.is_symlink() or pointer.is_symlink():
            raise ValueError("unsafe active execution pointer")
        sha = _hash(pointer.read_text(encoding="ascii"))
        target = self.root / sha
        if target.is_symlink() or (target / "manifest.json").is_symlink():
            raise ValueError("unsafe active execution manifest")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("composition_hash") != sha:
            raise ValueError("active execution manifest was modified")
        _verify_directory(target, manifest)
        return {**manifest, "directory": str(target)}
