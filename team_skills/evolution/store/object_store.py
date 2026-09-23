"""Shared storage helper functions."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from team_skills.library.bundle import (
    bundle_entrypoint_text,
    bundle_file_records,
    bundle_tree_sha256,
    coerce_skill_bundle,
)
from team_skills.library.publication import (
    active_skill_bundle_key,
    skill_version_bundle_key,
    skill_version_prefix,
    skill_version_record_key,
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


# --------------------------------------------------------------------------- #
# Evolution history (replaces evolve_history.jsonl; goes through the bucket  #
# so PG RLS / per-tenant Viking credentials enforce isolation automatically)  #
# --------------------------------------------------------------------------- #

_HISTORY_PREFIX = "evolve_history/"


def _history_key(timestamp: str, cycle_id: str) -> str:
    """Build a sortable, unique key for one history record."""
    ts = str(timestamp or "").replace(":", "").replace(".", "_")
    cid = str(cycle_id or "").replace("/", "_")
    return f"{_HISTORY_PREFIX}{ts}_{cid}.json"


def append_history_record(bucket, record: dict[str, Any]) -> None:
    """Write one evolution-cycle history record through the object store.

    Each cycle gets its own object key under ``evolve_history/`` so the
    existing per-tenant RLS / Viking account scoping applies without any
    new DDL.  The file-based ``evolve_history.jsonl`` remains as a fallback
    for single-tenant / local-backend deployments.
    """
    ts = str(record.get("timestamp") or "")
    cid = str(record.get("cycle_id") or "")
    if not ts and not cid:
        import uuid

        cid = uuid.uuid4().hex[:12]
    key = _history_key(ts, cid)
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    try:
        bucket.put_object(key, payload)
    except Exception as exc:  # noqa: BLE001 - history must not break the cycle
        logger.warning("[History] bucket write failed for %s: %s", key, exc)


def load_history_records(
    bucket,
    *,
    limit: int = 0,
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Read evolution-cycle records from the bucket, newest first.

    When *session_id* is set, only cycles referencing that session (in
    ``session_ids`` or any ``evolutions[].session_ids``) are returned.
    *limit* caps the result count (0 = all).
    """
    wanted = str(session_id or "").strip()
    records: list[dict[str, Any]] = []
    # Fast path: PG backend supports bulk fetch (single SQL round-trip),
    # avoiding N+1 queries that cost ~125ms each on remote PG.
    if hasattr(bucket, "iter_objects_bulk"):
        try:
            bulk = bucket.iter_objects_bulk(_HISTORY_PREFIX)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[History] bulk fetch failed: %s", exc)
            bulk = {}
        for key, content in bulk.items():
            if not key.endswith(".json"):
                continue
            try:
                record = json.loads(content.decode("utf-8")) if isinstance(content, bytes) else json.loads(content)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(record, dict):
                continue
            if wanted:
                ids = set(record.get("session_ids") or [])
                for evo in record.get("evolutions") or []:
                    if isinstance(evo, dict):
                        ids.update(evo.get("session_ids") or [])
                if wanted not in ids:
                    continue
            records.append(record)
    else:
        try:
            keys = list_object_keys(bucket, _HISTORY_PREFIX)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[History] bucket list failed: %s", exc)
            return []
        for key in keys:
            if not key.endswith(".json"):
                continue
            record = read_json_object(bucket, key)
            if not isinstance(record, dict):
                continue
            if wanted:
                ids = set(record.get("session_ids") or [])
                for evo in record.get("evolutions") or []:
                    if isinstance(evo, dict):
                        ids.update(evo.get("session_ids") or [])
                if wanted not in ids:
                    continue
            records.append(record)
    # Sort by timestamp descending (newest first).
    records.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    if limit and limit > 0:
        records = records[:limit]
    return records
