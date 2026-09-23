"""Content-addressed artifact helpers used by Replay runtime adapters."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from ._util import normalize_artifact_rel_path


class ReplayArtifactError(ValueError):
    """Raised when a Replay artifact payload is malformed."""


def skill_treatment_members(
    treatment: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return peer Skill bundles from a single- or multi-Skill treatment."""
    if not treatment:
        return []
    raw_members = treatment.get("skills")
    if isinstance(raw_members, list):
        members = [
            dict(item)
            for item in raw_members
            if isinstance(item, Mapping)
        ]
        if len(members) != len(raw_members):
            raise ReplayArtifactError("Skill set members must be objects")
        if not members:
            raise ReplayArtifactError("Skill set must contain at least one Skill")
        names = [str(item.get("name") or "").strip() for item in members]
        if any(not name for name in names):
            raise ReplayArtifactError("Every Skill set member requires a name")
        if len(set(names)) != len(names):
            raise ReplayArtifactError("Skill set member names must be unique")
        return members
    return [dict(treatment)]


def skill_treatment_content(treatment: Mapping[str, Any] | None) -> str:
    members = skill_treatment_members(treatment)
    if len(members) == 1:
        return str(members[0].get("content") or "")
    return "\n\n".join(
        f"# Skill: {member.get('name')}\n{member.get('content') or ''}"
        for member in members
    )


def artifact_tree_sha256(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        clean = normalize_artifact_rel_path(path)
        digest.update(clean.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def decode_bundle_payload(payload: Mapping[str, Any]) -> dict[str, bytes]:
    """Decode and verify the shared ``bundle_v1`` wire representation."""

    if str(payload.get("format") or "") != "bundle_v1":
        raise ReplayArtifactError("Unsupported bundle payload format")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ReplayArtifactError("Bundle payload files must be a list")
    bundle: dict[str, bytes] = {}
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise ReplayArtifactError("Bundle file entry must be an object")
        rel_path = normalize_artifact_rel_path(str(item.get("path") or ""))
        if rel_path in bundle:
            raise ReplayArtifactError(f"Duplicate bundle path: {rel_path}")
        encoding = str(item.get("encoding") or "utf-8").lower()
        content = item.get("content")
        if not isinstance(content, str):
            raise ReplayArtifactError(
                f"Bundle content must be text: {rel_path}"
            )
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ReplayArtifactError(
                    f"Invalid base64 bundle content: {rel_path}"
                ) from exc
        else:
            raise ReplayArtifactError(
                f"Unsupported bundle encoding {encoding!r}: {rel_path}"
            )
        declared_size = item.get("size")
        if declared_size is not None and int(declared_size) != len(data):
            raise ReplayArtifactError(f"Bundle size mismatch: {rel_path}")
        declared_sha = str(item.get("sha256") or "")
        actual_sha = hashlib.sha256(data).hexdigest()
        if declared_sha and declared_sha != actual_sha:
            raise ReplayArtifactError(f"Bundle hash mismatch: {rel_path}")
        bundle[rel_path] = data
    declared_tree = str(payload.get("tree_sha256") or "")
    if declared_tree and declared_tree != artifact_tree_sha256(bundle):
        raise ReplayArtifactError("Bundle tree hash mismatch")
    return bundle


def skill_treatment_files(skill: Mapping[str, Any]) -> dict[str, bytes]:
    """Materialize either a canonical bundle or a legacy inline Skill."""

    raw_bundle = skill.get("bundle")
    if isinstance(raw_bundle, Mapping):
        files = decode_bundle_payload(raw_bundle)
    else:
        raw_files = (
            skill.get("bundle_files")
            if isinstance(skill.get("bundle_files"), Mapping)
            else {}
        )
        files = {
            normalize_artifact_rel_path(str(path)): (
                bytes(content)
                if isinstance(content, (bytes, bytearray))
                else str(content).encode("utf-8")
            )
            for path, content in raw_files.items()
        }
    if "SKILL.md" not in files:
        name = str(skill.get("name") or "candidate-skill").strip()
        description = str(skill.get("description") or "").strip()
        category = str(skill.get("category") or "general").strip()
        content = str(skill.get("content") or "").strip()
        files["SKILL.md"] = (
            "---\n"
            f"name: {json.dumps(name, ensure_ascii=False)}\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            f"category: {json.dumps(category, ensure_ascii=False)}\n"
            "---\n\n"
            f"{content}\n"
        ).encode("utf-8")
    return files


def validate_skill_treatment(skill: Mapping[str, Any]) -> dict[str, Any]:
    try:
        members = skill_treatment_members(skill)
    except ReplayArtifactError as exc:
        return {"passed": False, "errors": [str(exc)]}
    if len(members) > 1 or isinstance(skill.get("skills"), list):
        errors: list[str] = []
        hashes: list[str] = []
        for member in members:
            result = validate_skill_treatment(member)
            if not result["passed"]:
                errors.extend(
                    f"{member.get('name') or '<unknown>'}: {error}"
                    for error in result["errors"]
                )
            hashes.append(
                f"{member.get('name')}:{result.get('tree_sha256') or ''}"
            )
        digest = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
        return {"passed": not errors, "errors": errors, "tree_sha256": digest}
    try:
        files = skill_treatment_files(skill)
        entrypoint = files.get("SKILL.md", b"").decode("utf-8")
    except (ReplayArtifactError, UnicodeDecodeError, TypeError, ValueError) as exc:
        return {"passed": False, "errors": [str(exc)]}
    errors = []
    if not str(skill.get("name") or "").strip():
        errors.append("Skill treatment name is required")
    if not entrypoint.strip():
        errors.append("Skill treatment SKILL.md is empty")
    return {
        "passed": not errors,
        "errors": errors,
        "tree_sha256": artifact_tree_sha256(files),
    }


def materialize_skill_treatment(
    skill: Mapping[str, Any],
    target: Path,
) -> str:
    files = skill_treatment_files(skill)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for rel_path, data in sorted(files.items()):
        path = target / Path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return artifact_tree_sha256(files)
