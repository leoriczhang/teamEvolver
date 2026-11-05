"""Compiler package integrity rules preserved from the original Skill miner."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml


def _skill_content_signature(content: str) -> tuple[Any, ...]:
    """Compare the fields preserved by OV SkillLoader, allowing YAML normalization."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not match:
        raise ValueError("Skill package requires SKILL.md YAML frontmatter")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError("Invalid SKILL.md YAML frontmatter") from exc
    if not isinstance(metadata, dict) or not all(
        isinstance(metadata.get(key), str) and metadata[key].strip() for key in ("name", "description")
    ):
        raise ValueError("SKILL.md requires a name and description")
    tools = metadata.get("allowed-tools", [])
    if isinstance(tools, str):
        parts, current, depth = [], [], 0
        for char in tools.strip():
            depth += (char == "(") - (char == ")")
            if depth < 0:
                raise ValueError("Invalid SKILL.md allowed-tools parentheses")
            if depth == 0 and (char.isspace() or char == ","):
                if current:
                    parts.append("".join(current))
                    current = []
            else:
                current.append(char)
        if depth:
            raise ValueError("Invalid SKILL.md allowed-tools parentheses")
        tools = [*parts, *(["".join(current)] if current else [])]
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        raise ValueError("Invalid SKILL.md allowed-tools")
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        tags = list(tags) if isinstance(tags, (tuple, set)) else [tags]
    return (
        metadata["name"], metadata["description"], tools, "allowed-tools" in metadata, tags,
        metadata.get("metadata"), match[2].strip(),
    )



def _skill_package_files(skill_name: str, skill_body: str, auxiliary_files: Mapping[str, str]) -> dict[str, bytes]:
    """Build only safe, bounded paths for OV's ZIP package and integrity limits."""
    if not isinstance(skill_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", skill_name):
        raise ValueError("Invalid Skill package name or body")
    if not isinstance(skill_body, str):
        raise ValueError("Invalid Skill package name or body")
    if _skill_content_signature(skill_body)[0] != skill_name:
        raise ValueError("SKILL.md name must match the published Skill name")
    if not isinstance(auxiliary_files, Mapping):
        raise ValueError("auxiliary_files must map relative paths to UTF-8 text")
    files = {"SKILL.md": skill_body.encode("utf-8")}
    reserved = {".abstract.md", ".overview.md", ".source.json"}
    for path, content in auxiliary_files.items():
        if (
            not isinstance(path, str) or not path or not isinstance(content, str)
            or any(char in path for char in "\\\x00%?#:") or any(ord(char) < 32 for char in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or len(path.split("/")) > 10 or path == "SKILL.md" or PurePosixPath(path).is_absolute()
            or PurePosixPath(path).name in reserved
        ):
            raise ValueError("Invalid Skill auxiliary file path or content")
        files[path] = content.encode("utf-8")
    directories = {str(parent) for path in files for parent in PurePosixPath(path).parents if str(parent) != "."}
    if directories.intersection(files):
        raise ValueError("Skill package file and directory paths conflict")
    # GET include_integrity uses 512 entries, 16 MiB per file and 64 MiB total.
    if len(files) + len(directories) + 2 > 512 or any(len(data) > 16 * 1024 * 1024 for data in files.values()):
        raise ValueError("Skill package exceeds OpenViking integrity limits")
    if sum(map(len, files.values())) > 64 * 1024 * 1024:
        raise ValueError("Skill package exceeds OpenViking integrity limits")
    return files



def _skill_package_matches(detail: Any, skill_body: str, files: dict[str, bytes]) -> bool:
    """Verify a locked OV manifest: file paths and raw UTF-8 SHA-256 values."""
    if not isinstance(detail, dict) or not isinstance(detail.get("content"), str):
        return False
    try:
        if _skill_content_signature(detail["content"]) != _skill_content_signature(skill_body):
            return False
    except ValueError:
        return False
    entries = detail.get("files")
    if not isinstance(entries, list):
        return False
    actual = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if entry.get("is_dir") is True:
            continue
        path = entry.get("path")
        if not isinstance(path, str) or path in actual:
            return False
        if PurePosixPath(path).name in {".abstract.md", ".overview.md"}:
            continue
        actual[path] = entry.get("sha256")
    expected = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    # OV rewrites SKILL.md through SkillLoader; compare its semantic body above,
    # then bind the manifest digest to the exact returned canonical content.
    expected["SKILL.md"] = hashlib.sha256(detail["content"].encode("utf-8")).hexdigest()
    return actual == expected
