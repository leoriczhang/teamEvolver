"""Portable Skill packages and bounded archives shared by every channel."""
from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from team_skills.library import editor, frontmatter
from team_skills.library.bundle import is_ignored_bundle_rel_path

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_CONTENT_BYTES = 128 * 1024 * 1024
MAX_FILES = 4096
MAX_SKILLS = 256


class TransferError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SkillPackage:
    name: str
    files: dict[str, bytes]
    origin: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportResult:
    metadata: dict[str, Any]
    content: bytes | None = None
    filename: str = ""


class TransferAdapter(Protocol):
    """Transport complete packages without mutating the Skill library."""
    def read(self, options: Mapping[str, Any]) -> list[SkillPackage]: ...
    def write(self, packages: Sequence[SkillPackage], options: Mapping[str, Any]) -> ExportResult: ...


def safe_path(value: str) -> str:
    value = value.replace("\\", "/")
    if (
        not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(c) < 32 for c in value)
    ):
        raise TransferError("Skill 包含不安全的相对路径")
    return value


def ignored(path: str) -> bool:
    return (
        any(p in {".git", "__MACOSX", ".clawhub", ".clawdhub"} or p.startswith("._")
            for p in PurePosixPath(path).parts)
        or is_ignored_bundle_rel_path(path)
    )


def package(name: str, files: Mapping[str, bytes], origin: dict[str, str] | None = None) -> SkillPackage:
    clean = {}
    for rel, data in files.items():
        rel = safe_path(rel)
        if not ignored(rel):
            clean[rel] = bytes(data)
    if len(clean) > MAX_FILES or sum(map(len, clean.values())) > MAX_CONTENT_BYTES:
        raise TransferError("Skill 文件数量或解压后大小超出限制", 413)
    if "SKILL.md" not in clean:
        raise TransferError("Skill 包中缺少 SKILL.md")
    try:
        raw = clean["SKILL.md"].decode("utf-8")
        fm = frontmatter._load_frontmatter_from_raw(raw) or {}
        if not isinstance(fm, dict) or not str(fm.get("description") or "").strip():
            raise TransferError("SKILL.md 必须包含 description")
        name = editor.validate_skill_name(name or str(fm.get("name") or ""))
    except (UnicodeError, editor.SkillEditorError) as exc:
        raise TransferError(str(exc)) from exc
    return SkillPackage(name, clean, origin or {})


def discover(files: Mapping[str, bytes], *, name: str = "", origin: dict[str, str] | None = None) -> list[SkillPackage]:
    roots = sorted(p[:-len("SKILL.md")] for p in files if p == "SKILL.md" or p.endswith("/SKILL.md"))
    if not roots:
        raise TransferError("未找到 SKILL.md")
    if len(roots) > MAX_SKILLS:
        raise TransferError(f"一次最多导入 {MAX_SKILLS} 个 Skill", 413)
    if name and len(roots) != 1:
        raise TransferError("仅单个 Skill 可以指定导入名称")
    result = []
    seen = set()
    for root in roots:
        # A nested Skill belongs to its own package, not to its parent.
        members = {
            p[len(root):]: data for p, data in files.items()
            if p.startswith(root) and not any(p.startswith(r) for r in roots if r != root and r.startswith(root))
        }
        fm = frontmatter._load_frontmatter_from_raw(members["SKILL.md"].decode("utf-8", errors="replace")) or {}
        if not isinstance(fm, dict):
            raise TransferError("SKILL.md frontmatter 必须是对象")
        inferred = name or str(fm.get("name") or "").strip() or root.rstrip("/").rsplit("/", 1)[-1]
        item = package(inferred, members, origin)
        if item.name in seen:
            raise TransferError(f"导入包含重复 Skill 名称：{item.name}")
        seen.add(item.name)
        result.append(item)
    return result


def read_zip(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise TransferError("ZIP 大小不能超过 64 MiB", 413)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_FILES:
                raise TransferError(f"ZIP 条目数量不能超过 {MAX_FILES}", 413)
            total = 0
            files = {}
            for entry in archive.infolist():
                rel = safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise TransferError("ZIP 不支持符号链接")
                if entry.flag_bits & 1:
                    raise TransferError("ZIP 不支持加密条目")
                total += entry.file_size
                if total > MAX_CONTENT_BYTES:
                    raise TransferError("ZIP 解压后大小不能超过 128 MiB", 413)
                if entry.is_dir() or ignored(rel):
                    continue
                if rel in files:
                    raise TransferError("ZIP 包含重复文件路径")
                files[rel] = archive.read(entry)
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise TransferError("无法读取 ZIP，请检查压缩包格式及完整性") from exc


def read_directory(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise TransferError("仓库中的 Skill 路径不是有效目录")
    files = {}
    total = 0
    for path in root.rglob("*"):
        rel = safe_path(path.relative_to(root).as_posix())
        if ignored(rel):
            continue
        if path.is_symlink():
            raise TransferError("Skill 目录不支持符号链接")
        if not path.is_file():
            continue
        total += path.stat().st_size
        if len(files) >= MAX_FILES or total > MAX_CONTENT_BYTES:
            raise TransferError("Skill 文件数量或大小超出限制", 413)
        data = path.read_bytes()
        files[rel] = data
    return files


def make_zip(packages: Sequence[SkillPackage], *, wrapped: bool = True) -> bytes:
    if not packages or len(packages) > MAX_SKILLS:
        raise TransferError(f"请选择 1–{MAX_SKILLS} 个 Skill")
    if sum(len(p.files) for p in packages) > MAX_FILES:
        raise TransferError("导出文件数量超出限制", 413)
    if sum(len(data) for p in packages for data in p.files.values()) > MAX_CONTENT_BYTES:
        raise TransferError("导出内容大小超出限制", 413)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in packages:
            for rel, data in sorted(item.files.items()):
                archive.writestr(f"{item.name}/{rel}" if wrapped else rel, data)
    content = buf.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise TransferError("导出 ZIP 大小超出 64 MiB 限制", 413)
    return content
