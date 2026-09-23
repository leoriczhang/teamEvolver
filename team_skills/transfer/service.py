"""Unified Skill transfer interface: validation, conflicts, versions and adapters."""
from __future__ import annotations

import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from team_skills.library import editor
from team_skills.library.bundle import bundle_tree_sha256, write_skill_bundle
from team_skills.library.mutations import SkillMutationCommand, SkillMutationService

from .channels import MarketplaceAdapter, ZipAdapter
from .git import GitAdapter
from .packages import (
    MAX_CONTENT_BYTES,
    MAX_FILES,
    MAX_SKILLS,
    ExportResult,
    TransferAdapter,
    TransferError,
    package,
    read_directory,
)

_IMPORT_LOCK = threading.RLock()


class SkillTransferService:
    """Callers choose a channel; all local and versioned behavior stays here."""
    def __init__(self, skills_dir: str, mutations: SkillMutationService,
                 adapters: Mapping[str, TransferAdapter] | None = None):
        self.skills_dir = skills_dir
        self.mutations = mutations
        self.hub = mutations._hub
        self.adapters = dict(adapters) if adapters is not None else {
            "zip": ZipAdapter(), "marketplace": MarketplaceAdapter(), "git": GitAdapter(),
        }

    def _adapter(self, channel: str) -> TransferAdapter:
        try:
            return self.adapters[channel]
        except KeyError as exc:
            raise TransferError(f"不支持的传输渠道：{channel}") from exc

    def list_skills(self) -> list[dict[str, Any]]:
        skills = {item["name"]: item for item in editor.list_skills(self.skills_dir)}
        for item in self.hub.list_remote():
            skills[item["name"]] = {"name": item["name"], "description": item.get("description", ""),
                                    "version": item.get("version", 0), "file_count": len(item.get("files") or [])}
        return sorted(skills.values(), key=lambda item: item["name"])

    def import_skills(self, channel: str, options: Mapping[str, Any], *, conflict: str = "replace") -> dict[str, Any]:
        if conflict not in {"replace", "skip", "error"}:
            raise TransferError("conflict 必须是 replace、skip 或 error")
        # Download and validate everything before touching storage.
        packages = self._adapter(channel).read(options)
        if not packages or len(packages) > MAX_SKILLS:
            raise TransferError(f"请选择 1–{MAX_SKILLS} 个 Skill")
        packages = [package(p.name, p.files, p.origin) for p in packages]
        names = [p.name for p in packages]
        if len(names) != len(set(names)):
            raise TransferError("导入包含重复 Skill 名称")
        self._check_size(packages)
        result: dict[str, Any] = {"channel": channel, "imported": [], "skipped": [], "errors": []}
        with _IMPORT_LOCK:
            remote = {item["name"]: item for item in self.hub.list_remote()}
            local = {item["name"] for item in editor.list_skills(self.skills_dir)}
            existing = local | remote.keys()
            if conflict == "error" and any(name in existing for name in names):
                raise TransferError("存在同名 Skill；请选择替换或跳过", 409)
            for item in packages:
                if item.name in existing and conflict == "skip":
                    result["skipped"].append({"name": item.name, "reason": "exists"})
                    continue
                committed = False
                try:
                    # Stage a full bundle; publishing always crosses SkillMutationService.
                    with tempfile.TemporaryDirectory(prefix="te-skill-import-") as tmp:
                        write_skill_bundle(Path(tmp) / item.name, item.files)
                        commit = self.mutations.execute(SkillMutationCommand(
                            action="update" if item.name in existing else "publish", name=item.name,
                            mutation_id=f"transfer-{uuid.uuid4().hex}", skills_dir=tmp,
                            metadata={"channel": channel, "origin": item.origin},
                        ))
                    committed = True
                    self._install(item.name, item.files)
                    record = commit.get("expected") or {}
                    unchanged = commit.get("status") == "unchanged"
                    entry = {"name": item.name, "created": item.name not in existing,
                             "status": "unchanged" if unchanged else "updated" if item.name in existing else "created",
                             "version": record.get("version", 0), "files": sorted(item.files),
                             "tree_sha256": bundle_tree_sha256(item.files), "origin": item.origin,
                             "cloud": {"synced": True, "event_id": commit.get("event_id", "")}}
                    result["imported"].append(entry)
                except Exception:
                    # Backend exceptions may contain credentials; expose only stable errors.
                    message = "已记录版本，本地缓存更新失败" if committed else "导入版本失败，请检查存储状态"
                    result["errors"].append({"name": item.name, "stored": committed, "error": message})
        return result

    def _install(self, name: str, files: Mapping[str, bytes]) -> None:
        root = Path(self.skills_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = Path(editor.find_skill_dir(self.skills_dir, name) or root / name)
        if not target.is_absolute():
            target = Path.cwd() / target
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise TransferError("本地 Skill 目录不安全")
        # Rename on the same filesystem; preserve the old bundle if installation fails.
        with tempfile.TemporaryDirectory(prefix=".skill-transfer-", dir=root) as tmp:
            stage, backup = Path(tmp) / "stage", Path(tmp) / "backup"
            write_skill_bundle(stage, files)
            had_old = target.exists()
            if had_old:
                os.replace(target, backup)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage, target)
            except BaseException:
                if had_old:
                    os.replace(backup, target)
                raise

    @staticmethod
    def _check_size(packages) -> None:
        if (sum(len(p.files) for p in packages) > MAX_FILES
                or sum(len(v) for p in packages for v in p.files.values()) > MAX_CONTENT_BYTES):
            raise TransferError("Skill 文件数量或大小超出限制", 413)

    def export_skills(self, channel: str, names: Sequence[str], options: Mapping[str, Any]) -> ExportResult:
        adapter = self._adapter(channel)
        if not names or len(names) > MAX_SKILLS or len(names) != len(set(names)):
            raise TransferError(f"请选择 1–{MAX_SKILLS} 个不重复的 Skill")
        remote = {item["name"]: item for item in self.hub.list_remote()}
        packages = []
        for name in names:
            try:
                name = editor.validate_skill_name(name)
            except editor.SkillEditorError as exc:
                raise TransferError(str(exc)) from exc
            record = remote.get(name)
            if record:
                version = int(record.get("version") or 0)
                files = (self.hub._read_version_bundle(name, version) if version
                         else self.hub._download_skill_bundle(name, record))
            else:
                directory = editor.find_skill_dir(self.skills_dir, name)
                if not directory:
                    raise TransferError(f"Skill 不存在：{name}", 404)
                files = read_directory(Path(directory))
            packages.append(package(name, files))
            self._check_size(packages)
        return adapter.write(packages, options)
