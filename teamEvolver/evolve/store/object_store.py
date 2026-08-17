"""Shared storage helper functions."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from ...skills.bundle import (
    bundle_entrypoint_text,
    bundle_file_records,
    bundle_tree_sha256,
    coerce_skill_bundle,
)

logger = logging.getLogger(__name__)


def list_session_keys(bucket, prefix: str) -> list[str]:
    """List all session ``*.json`` objects.

    When *prefix* is provided, only that customer/agent prefix is scanned
    (for example ``peers/customer-a/`` -> ``peers/customer-a/sessions/``).
    When *prefix* is empty, scan the whole agent namespace and include both
    agent-level ``sessions/...`` and peer-level ``peers/*/sessions/...``.
    """
    list_prefix = f"{prefix}sessions/" if prefix else ""
    iterator = bucket.iter_objects(prefix=list_prefix)
    keys: list[str] = []
    for obj in iterator:
        if not obj.key.endswith(".json"):
            continue
        if prefix or obj.key.startswith("sessions/") or "/sessions/" in obj.key:
            keys.append(obj.key)
    return keys


def list_object_keys(bucket, prefix: str) -> list[str]:
    """List all object keys under *prefix* across local/viking backends."""
    iterator = bucket.iter_objects(prefix=prefix)
    return [obj.key for obj in iterator]


def read_json_object(bucket, key: str) -> Optional[dict]:
    """Download and parse a single JSON object from storage."""
    try:
        data = bucket.get_object(key).read().decode("utf-8")
        return json.loads(data)
    except Exception as e:
        logger.warning("[Storage] failed to read %s: %s", key, e)
        return None


def load_manifest(bucket, prefix: str) -> dict[str, dict[str, Any]]:
    """Load ``manifest.json`` from storage. Returns ``{skill_name: record}``."""
    key = f"{prefix}manifest.json"
    try:
        data = bucket.get_object(key).read().decode("utf-8")
    except Exception:
        return {}

    skills: dict[str, dict[str, Any]] = {}
    for line in data.strip().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            name = rec.get("name", "")
            if name:
                skills[name] = rec
        except json.JSONDecodeError:
            continue
    return skills


def load_manifest_snapshot(
    bucket,
    prefix: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Read a manifest and the exact CAS precondition for that same payload."""
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
    newline = bytes([10])
    return b"".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") + newline
        for record in manifest.values()
    )


def save_manifest(bucket, prefix: str, manifest: dict[str, dict[str, Any]]) -> None:
    """Write the full manifest back to storage."""
    lines = [json.dumps(rec, ensure_ascii=False) for rec in manifest.values()]
    content = "\n".join(lines) + "\n" if lines else ""
    bucket.put_object(f"{prefix}manifest.json", content.encode("utf-8"))


def delete_session_keys(bucket, keys: list[str]) -> int:
    """Delete session objects from the bucket.

    Returns the number of successfully deleted keys.
    """
    deleted = 0
    for key in keys:
        try:
            bucket.delete_object(key)
            deleted += 1
        except Exception as e:
            logger.warning("[Storage] failed to delete %s: %s", key, e)
    if deleted:
        logger.info("[Storage] deleted %d/%d session keys", deleted, len(keys))
    return deleted


def fetch_skill_content(bucket, prefix: str, skill_name: str) -> Optional[str]:
    """Download a single ``SKILL.md`` from storage."""
    key = f"{prefix}skills/{skill_name}/SKILL.md"
    try:
        return bucket.get_object(key).read().decode("utf-8")
    except Exception:
        return None


def fetch_skill_bundle(
    bucket,
    prefix: str,
    skill_name: str,
    record: Optional[dict[str, Any]] = None,
) -> dict[str, bytes]:
    """Download a full skill bundle from storage.

    Backward compatibility:
      - bundle-aware records read nested files from ``skills/<name>/files/...``
      - legacy records fall back to a single ``SKILL.md``
    """
    bundle: dict[str, bytes] = {}
    file_entries = (record or {}).get("files")
    if isinstance(file_entries, list) and file_entries:
        for item in file_entries:
            rel_path = str((item or {}).get("path") or "").strip().replace("\\", "/")
            if not rel_path:
                continue
            if rel_path == "SKILL.md":
                key = f"{prefix}skills/{skill_name}/SKILL.md"
            else:
                key = f"{prefix}skills/{skill_name}/files/{rel_path}"
            bundle[rel_path] = bucket.get_object(key).read()
        return bundle

    content = fetch_skill_content(bucket, prefix, skill_name)
    if content is None:
        return {}
    bundle["SKILL.md"] = content.encode("utf-8")
    return bundle


def fetch_skill_bundle_text(
    bucket,
    prefix: str,
    skill_name: str,
    record: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Download the bundle and return its ``SKILL.md`` entrypoint text."""
    bundle = fetch_skill_bundle(bucket, prefix, skill_name, record)
    if not bundle:
        return None
    try:
        return bundle_entrypoint_text(bundle)
    except Exception:
        return None


def active_skill_bundle_key(prefix: str, skill_name: str, rel_path: str) -> str:
    clean = str(rel_path or "").strip().replace("\\", "/")
    if clean == "SKILL.md":
        return f"{prefix}skills/{skill_name}/SKILL.md"
    return f"{prefix}skills/{skill_name}/files/{clean}"


def save_active_bundle(
    bucket,
    prefix: str,
    skill_name: str,
    bundle_files: dict[str, bytes],
) -> dict[str, Any]:
    """Write and verify a complete active bundle, then remove stale extras."""
    bundle = coerce_skill_bundle(bundle_files)
    keep_keys: set[str] = set()
    stored: dict[str, bytes] = {}
    for rel_path, data in sorted(bundle.items()):
        key = active_skill_bundle_key(prefix, skill_name, rel_path)
        keep_keys.add(key)
        bucket.put_object(key, data)
        stored[rel_path] = bucket.get_object(key).read()
    extras_prefix = f"{prefix}skills/{skill_name}/files/"
    for key in list_object_keys(bucket, extras_prefix):
        if key not in keep_keys:
            bucket.delete_object(key)
    return {
        "format": "bundle_v1",
        "entrypoint": "SKILL.md",
        "tree_sha256": bundle_tree_sha256(stored),
        "files": bundle_file_records(stored),
    }


def skill_version_prefix(prefix: str, skill_name: str, version: int) -> str:
    return f"{prefix}skills/{skill_name}/versions/v{max(1, int(version or 1))}/"


def skill_version_bundle_key(prefix: str, skill_name: str, version: int, rel_path: str) -> str:
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


def publish_skill_bundle_batch(
    bucket,
    prefix: str,
    skill_name: str,
    version: int,
    bundle_files: dict[str, bytes],
    *,
    manifest: dict[str, dict[str, Any]],
    registry_bytes: bytes,
    fixed_preconditions: Optional[dict[str, dict[str, str]]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Conditionally publish the live bundle, version, manifest, and registry."""
    if not bool(getattr(bucket, "native_batch_write", False)):
        raise TypeError("bucket does not support native batch_write")

    bundle = coerce_skill_bundle(bundle_files)
    record = build_bundle_record(bundle)
    objects: dict[str, bytes] = {}
    live_keys: set[str] = set()
    version_keys: set[str] = set()
    for rel_path, data in sorted(bundle.items()):
        live_key = active_skill_bundle_key(prefix, skill_name, rel_path)
        version_key = skill_version_bundle_key(prefix, skill_name, version, rel_path)
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
            "skill bundle is too large for one OpenViking batch-write; "
            "reduce the bundle below 127 files"
        )
    preconditions = {key: bucket.object_precondition(key) for key in objects}
    preconditions.update(fixed_preconditions or {})
    result = bucket.batch_write(
        objects,
        preconditions=preconditions,
        wait=True,
        telemetry=True,
    )

    for key in list_object_keys(bucket, f"{prefix}skills/{skill_name}/files/"):
        if key not in live_keys:
            bucket.delete_object(key)
    for key in list_object_keys(
        bucket,
        f"{skill_version_prefix(prefix, skill_name, version)}files/",
    ):
        if key not in version_keys:
            bucket.delete_object(key)
    return record, result


def list_skill_versions(bucket, prefix: str, skill_name: str) -> list[int]:
    """Return the sorted list of archived version numbers for a skill.

    Versions are discovered by listing keys under ``skills/<name>/versions/``
    and parsing the ``vN`` segment, so this reflects whatever bundles were
    actually archived by :func:`save_version_bundle`.
    """
    base = f"{prefix}skills/{skill_name}/versions/"
    versions: set[int] = set()
    for key in list_object_keys(bucket, base):
        remainder = key[len(base):]
        segment = remainder.split("/", 1)[0]
        if not segment.startswith("v"):
            continue
        try:
            versions.add(int(segment[1:]))
        except ValueError:
            continue
    return sorted(versions)


def save_version_bundle(
    bucket,
    prefix: str,
    skill_name: str,
    version: int,
    bundle_files: dict[str, bytes],
) -> dict[str, Any]:
    bundle = coerce_skill_bundle(bundle_files)
    keep_keys: set[str] = set()
    stored_bundle: dict[str, bytes] = {}
    for rel_path, data in sorted(bundle.items()):
        key = skill_version_bundle_key(prefix, skill_name, version, rel_path)
        keep_keys.add(key)
        bucket.put_object(key, data)
        stored_bundle[rel_path] = bucket.get_object(key).read()
    for key in list_object_keys(bucket, f"{skill_version_prefix(prefix, skill_name, version)}files/"):
        if key not in keep_keys:
            bucket.delete_object(key)
    record = {
        "format": "bundle_v1",
        "entrypoint": "SKILL.md",
        "tree_sha256": bundle_tree_sha256(stored_bundle),
        "files": bundle_file_records(stored_bundle),
    }
    bucket.put_object(
        skill_version_record_key(prefix, skill_name, version),
        json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return record


def load_version_bundle_record(
    bucket,
    prefix: str,
    skill_name: str,
    version: int,
) -> Optional[dict[str, Any]]:
    try:
        payload = bucket.get_object(skill_version_record_key(prefix, skill_name, version)).read().decode("utf-8")
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_version_bundle(
    bucket,
    prefix: str,
    skill_name: str,
    version: int,
    record: Optional[dict[str, Any]] = None,
) -> dict[str, bytes]:
    bundle: dict[str, bytes] = {}
    version_record = record or load_version_bundle_record(bucket, prefix, skill_name, version) or {}
    file_entries = version_record.get("files")
    if isinstance(file_entries, list) and file_entries:
        for item in file_entries:
            rel_path = str((item or {}).get("path") or "").strip().replace("\\", "/")
            if not rel_path:
                continue
            key = skill_version_bundle_key(prefix, skill_name, version, rel_path)
            bundle[rel_path] = bucket.get_object(key).read()
        return bundle

    try:
        bundle["SKILL.md"] = bucket.get_object(skill_version_bundle_key(prefix, skill_name, version, "SKILL.md")).read()
    except Exception:
        return {}
    return bundle
