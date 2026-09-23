"""Atomic persistence primitives for versioned Skill bundles."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from team_skills.library.bundle import (
    bundle_file_records,
    bundle_tree_sha256,
    coerce_skill_bundle,
)


def active_skill_bundle_key(prefix: str, skill_name: str, rel_path: str) -> str:
    clean = str(rel_path or "").strip().replace("\\", "/")
    if clean == "SKILL.md":
        return f"{prefix}skills/{skill_name}/SKILL.md"
    return f"{prefix}skills/{skill_name}/files/{clean}"


def skill_version_prefix(prefix: str, skill_name: str, version: int) -> str:
    return f"{prefix}skills/{skill_name}/versions/v{max(1, int(version or 1))}/"


def skill_version_bundle_key(
    prefix: str,
    skill_name: str,
    version: int,
    rel_path: str,
) -> str:
    clean = str(rel_path or "").strip().replace("\\", "/")
    base = skill_version_prefix(prefix, skill_name, version)
    if clean == "SKILL.md":
        return f"{base}SKILL.md"
    return f"{base}files/{clean}"


def skill_version_record_key(prefix: str, skill_name: str, version: int) -> str:
    return f"{skill_version_prefix(prefix, skill_name, version)}bundle.json"


def build_bundle_record(bundle_files: dict[str, bytes]) -> dict[str, Any]:
    bundle = coerce_skill_bundle(bundle_files)
    return {
        "format": "bundle_v1",
        "entrypoint": "SKILL.md",
        "tree_sha256": bundle_tree_sha256(bundle),
        "files": bundle_file_records(bundle),
    }


def load_manifest_snapshot(
    bucket: Any,
    prefix: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Read a manifest and return a CAS precondition for the exact payload."""
    key = f"{prefix}manifest.json"
    try:
        raw = bucket.get_object(key).read()
    except FileNotFoundError:
        return {}, {"kind": "create_if_absent"}

    manifest: dict[str, dict[str, Any]] = {}
    for line in raw.decode("utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(record.get("name") or "") if isinstance(record, dict) else ""
        if name:
            manifest[name] = record
    return manifest, {
        "kind": "replace_if_hash",
        "base_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def serialize_manifest(manifest: dict[str, dict[str, Any]]) -> bytes:
    newline = b"\n"
    return b"".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") + newline
        for record in manifest.values()
    )


def publish_skill_bundle_batch(
    bucket: Any,
    prefix: str,
    skill_name: str,
    version: int,
    bundle_files: dict[str, bytes],
    *,
    manifest: dict[str, dict[str, Any]],
    registry_bytes: bytes,
    fixed_preconditions: Optional[dict[str, dict[str, str]]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Conditionally publish live/version bundles, manifest and registry."""
    if not bool(getattr(bucket, "native_batch_write", False)):
        raise TypeError("bucket does not support native batch_write")

    bundle = coerce_skill_bundle(bundle_files)
    record = build_bundle_record(bundle)
    objects: dict[str, bytes] = {}
    live_keys: set[str] = set()
    version_keys: set[str] = set()
    for rel_path, data in sorted(bundle.items()):
        live_key = active_skill_bundle_key(prefix, skill_name, rel_path)
        version_key = skill_version_bundle_key(
            prefix,
            skill_name,
            version,
            rel_path,
        )
        live_keys.add(live_key)
        version_keys.add(version_key)
        objects[live_key] = data
        objects[version_key] = data

    version_record_key = skill_version_record_key(prefix, skill_name, version)
    manifest_key = f"{prefix}manifest.json"
    registry_key = f"{prefix}evolve_skill_registry.json"
    objects[version_record_key] = json.dumps(
        record,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    objects[manifest_key] = serialize_manifest(manifest)
    objects[registry_key] = registry_bytes

    if len(objects) > 256:
        raise ValueError(
            "skill bundle is too large for one transactional batch; "
            "reduce the bundle below 127 files"
        )
    preconditions = {
        key: bucket.object_precondition(key) for key in objects
    }
    preconditions.update(fixed_preconditions or {})
    result = bucket.batch_write(
        objects,
        preconditions=preconditions,
        wait=True,
        telemetry=True,
    )

    for obj in bucket.iter_objects(
        prefix=f"{prefix}skills/{skill_name}/files/"
    ):
        if obj.key not in live_keys:
            bucket.delete_object(obj.key)
    for obj in bucket.iter_objects(
        prefix=f"{skill_version_prefix(prefix, skill_name, version)}files/"
    ):
        if obj.key not in version_keys:
            bucket.delete_object(obj.key)
    return record, result
