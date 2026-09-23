#!/usr/bin/env python3
"""Offline phase-6 data cleanup, after phase 5 and a separate stable window.

Only --apply deletes agents.json. Default dry-run is read-only. Phase-5 backups
remain immutable. --restore restores the state immediately before phase 6.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import migrate_deregister as phase5
from teamEvolver.storage.admin_kv import _pg_store, kv_key

VERSION = "agent-deregister-phase6-v1"
MANIFEST = "deregister-phase6.manifest.json"


def delete_agents(config, tenant):
    """Delete the live registry in precisely the same scope as its backup."""
    store = _pg_store(config, tenant)
    if store is None:
        phase5.location(config, phase5.AGENTS, tenant).unlink(missing_ok=True)
    else:
        store.delete_object(kv_key(phase5.AGENTS))
    if phase5.read(config, phase5.AGENTS, tenant):
        raise RuntimeError(f"registry deletion verification failed: {tenant}")


def clean_context(data, tenant):
    if data.get("deregister_migration_version") != phase5.VERSION:
        raise ValueError(f"phase5 Context migration missing: {tenant}")
    cleaned = copy.deepcopy(data)
    for group in ("refs", "sessions", "snapshots"):
        records = cleaned.get(group, {})
        if not isinstance(records, dict):
            raise ValueError(f"invalid Context group: {tenant}/{group}")
        for record in records.values():
            if not isinstance(record, dict) or record.get("tenant_id") != tenant:
                raise ValueError(f"Context tenant binding missing: {tenant}/{group}")
            if phase5.normalize_user_id(record.get("user_id")) != record["user_id"]:
                raise ValueError("Context user_id needs manual repair")
            if "agent_id" in record:
                raise ValueError("legacy writer detected; repeat phase5 preflight")
            record.pop("agent_id_legacy", None)
    cleaned["deregister_cleanup_version"] = VERSION
    return cleaned


def validate(config, manifest):
    if manifest.get("version") != VERSION or manifest.get("scope") != phase5.scope(config):
        raise ValueError("phase6 manifest version/backend/scope mismatch")
    tenants = manifest.get("tenants")
    if not isinstance(tenants, list) or not tenants or len(set(tenants)) != len(tenants):
        raise ValueError("invalid phase6 tenants")
    from teamEvolver.storage.pg_store import validate_tenant_id
    for tenant in tenants:
        validate_tenant_id(tenant)
    expected = {(tid, key) for tid in tenants for key in (phase5.AGENTS, phase5.CONTEXT)}
    seen = set()
    for entry in manifest.get("objects", []):
        pair = (entry["tenant"], entry["key"])
        if pair not in expected or pair in seen or entry["backup_key"] != entry["key"].removesuffix(".json") + ".pre-phase6.json":
            raise ValueError("invalid phase6 object scope")
        seen.add(pair)
    if seen != expected:
        raise ValueError("incomplete phase6 manifest")


def finalize(config, phase5_manifest, *, apply=False, evidence=None, registry=None):
    phase5.validate_manifest(config, phase5_manifest)
    registry = registry or phase5.TenantRegistry(config)
    tenants = sorted({t.tenant_id for t in registry.list_tenants()} | {"default"})
    if tenants != sorted(phase5_manifest["tenants"]) or not phase5_manifest.get("global_users_cleaned"):
        raise ValueError("phase6 requires an applied all-tenant phase5 manifest")
    if not config.storage_pg_enabled and tenants != ["default"]:
        raise ValueError("file mode supports only default")
    persisted = {k: v for k, v in phase5_manifest.items() if k != "action"}
    for tenant in tenants:
        if phase5.read(config, phase5_manifest["manifest_key"], tenant) != persisted:
            raise ValueError(f"applied phase5 manifest missing: {tenant}")
    for entry in phase5_manifest["objects"]:
        backup = phase5.read(config, entry["backup_key"], entry["tenant"])
        if phase5.checksum(backup) != entry["before_sha256"]:
            raise ValueError("phase5 backup checksum mismatch")
    users = phase5.read(config, phase5.USERS, "default")
    if users.get("deregister_migration_version") != phase5.VERSION or any(
        "agent_subjects" in u or "agent_identities" in u for u in users.get("users", [])
    ):
        raise ValueError("global user mappings have not been retired")
    if apply:
        phase5.verify_evidence(config, evidence or {}, tenants)
        if (evidence or {}).get("phase5_stable_cycle_observed") is not True:
            raise ValueError("phase5_stable_cycle_observed evidence required for separate cleanup release")
    current = {(tid, key): phase5.read(config, key, tid)
               for tid in tenants for key in (phase5.AGENTS, phase5.CONTEXT)}
    existing = phase5.read(config, MANIFEST, "default")
    if existing:
        validate(config, existing)
        if existing["tenants"] != tenants or existing["phase5_sha256"] != phase5.checksum(persisted):
            raise ValueError("phase6 manifest selection changed")
        manifest = existing
    else:
        for entry in phase5_manifest["objects"]:
            if entry["key"] == phase5.AGENTS and phase5.checksum(current[entry["tenant"], phase5.AGENTS]) != entry["after_sha256"]:
                raise ValueError("registry changed after phase5; legacy writer may still be active")
        manifest = {"version": VERSION, "scope": phase5.scope(config), "tenants": tenants,
                    "phase5_sha256": phase5.checksum(persisted), "objects": []}
        for (tid, key), before in current.items():
            after = clean_context(before, tid) if key == phase5.CONTEXT else {}
            manifest["objects"].append({
                "tenant": tid, "key": key, "backup_key": key.removesuffix(".json") + ".pre-phase6.json",
                "before_sha256": phase5.checksum(before), "after_sha256": phase5.checksum(after),
                "before_count": phase5.counts(before), "after_count": phase5.counts(after),
            })
    prepared = []
    for entry in manifest["objects"]:
        tid, key = entry["tenant"], entry["key"]
        before = current[tid, key]
        if phase5.checksum(before) not in {entry["before_sha256"], entry["after_sha256"]}:
            raise ValueError(f"source changed since phase6 backup: {tid}/{key}")
        after = clean_context(before, tid) if key == phase5.CONTEXT else {}
        if phase5.checksum(after) != entry["after_sha256"]:
            raise ValueError("phase6 transformation changed")
        backup = phase5.read(config, entry["backup_key"], tid)
        if (backup or existing) and phase5.checksum(backup) != entry["before_sha256"]:
            raise ValueError(f"phase6 backup checksum mismatch: {tid}/{key}")
        prepared.append((entry, before, after))
    if not apply:
        return {**manifest, "action": "dry-run"}
    for entry, before, after in prepared:
        if not existing:
            phase5.write(config, entry["backup_key"], entry["tenant"], before)
    for tid in tenants:
        phase5.write(config, MANIFEST, tid, manifest)
    # Validate/migrate all Context objects first. Delete registries only after
    # every tenant's Context write has succeeded.
    for key in (phase5.CONTEXT, phase5.AGENTS):
        for entry, before, after in prepared:
            if entry["key"] != key:
                continue
            tid = entry["tenant"]
            if phase5.checksum(phase5.read(config, key, tid)) != phase5.checksum(before):
                raise ValueError(f"concurrent writer detected: {tid}/{key}")
            if key == phase5.AGENTS:
                delete_agents(config, tid)
            elif before != after:
                phase5.write(config, key, tid, after)
    return {**manifest, "action": "applied"}


def restore(config, manifest, *, writers_stopped=False):
    if not writers_stopped:
        raise ValueError("restore requires --writers-stopped")
    validate(config, manifest)
    prepared = []
    for entry in manifest["objects"]:
        tid, key = entry["tenant"], entry["key"]
        backup = phase5.read(config, entry["backup_key"], tid)
        if phase5.checksum(backup) != entry["before_sha256"] or phase5.counts(backup) != entry["before_count"]:
            raise ValueError("phase6 backup checksum/count mismatch")
        current = phase5.read(config, key, tid)
        if phase5.checksum(current) not in {entry["before_sha256"], entry["after_sha256"]}:
            raise ValueError("newer source data exists; reconcile offline before restore")
        prepared.append((tid, key, backup, current))
    for tid, key, backup, current in prepared:
        if phase5.read(config, key, tid) != current:
            raise ValueError("concurrent writer detected")
        phase5.write(config, key, tid, backup)
    return {**manifest, "action": "restored"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--phase5-manifest", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--restore", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        config = phase5.ConfigStore(args.config).to_config()
        if args.restore:
            result = restore(config, json.loads(args.restore.read_text()), writers_stopped=args.writers_stopped)
        else:
            if not args.phase5_manifest:
                parser.error("--phase5-manifest is required")
            result = finalize(config, json.loads(args.phase5_manifest.read_text()), apply=args.apply,
                              evidence=json.loads(args.evidence.read_text()) if args.evidence else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
