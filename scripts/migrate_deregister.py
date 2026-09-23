#!/usr/bin/env python3
"""Offline, scoped phase-5 migration. Backups are immutable JSON objects.

Checksums cover canonical JSON (not whitespace). Stop writers for apply/restore.
No deployment is performed. --dry-run is the default and writes nothing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from teamEvolver.config_store import ConfigStore
from teamEvolver.integrations.agent_principal import normalize_user_id
from teamEvolver.integrations.context_workspace import ContextStateStore
from teamEvolver.storage.admin_kv import read_kv, write_kv
from teamEvolver.tenants.registry import TenantRegistry

VERSION = "agent-deregister-v1"
CONTEXT = "agent_context_state.json"
USERS = "users.json"
AGENTS = "agents.json"
NAMES = {CONTEXT, USERS, AGENTS}


def checksum(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def counts(data):
    return {key: len(value) for key, value in data.items() if isinstance(value, (list, dict))}


def location(config, name, tenant):
    users = Path(config.users_registry_path).expanduser() if config.users_registry_path else Path.home() / ".teamEvolver/users.json"
    if name == USERS or name == "users.pre-deregister.json":
        if tenant != "default":
            raise ValueError("users are global; explicit default scope required")
        return users if name == USERS else users.with_name(name)
    context = ContextStateStore(config, tenant_id=tenant).path
    if name in {AGENTS, "agents.pre-deregister.json"}:
        # Legacy file backend only supports default; PG paths are not used.
        return context.with_name(name)
    return context.with_name(name)


def scope(config):
    if config.storage_pg_enabled:
        from teamEvolver.storage.pg_pool import dsn_from_env
        return {"backend": "postgres", "schema": config.storage_pg_schema,
                "database_sha256": checksum(config.storage_pg_dsn or dsn_from_env())}
    return {"backend": "file", "users_path": str(location(config, USERS, "default").resolve()),
            "context_path": str(location(config, CONTEXT, "default").resolve())}


def read(config, key, tenant):
    return read_kv(config, key, location(config, key, tenant), tenant_id=tenant)


def write(config, key, tenant, value):
    write_kv(config, key, location(config, key, tenant), value, tenant_id=tenant)
    if checksum(read(config, key, tenant)) != checksum(value):
        raise RuntimeError(f"write verification failed: {tenant}/{key}")


def migrate_context(data, tenant):
    migrated = copy.deepcopy(data)
    for group in ("refs", "sessions", "snapshots"):
        records = migrated.get(group, {})
        if not isinstance(records, dict):
            raise ValueError(f"{tenant}/{group}: expected object")
        for record_id, record in records.items():
            if not isinstance(record, dict):
                raise ValueError(f"{tenant}/{group}/{record_id}: expected object")
            user = record.get("user_id")
            if normalize_user_id(user) != user:
                raise ValueError(f"{tenant}/{group}/{record_id}: user_id needs manual repair")
            if record.get("tenant_id") not in (None, tenant):
                raise ValueError(f"{tenant}/{group}/{record_id}: tenant conflict")
            record["tenant_id"] = tenant
            if "agent_id" in record:
                if "agent_id_legacy" in record and record["agent_id_legacy"] != record["agent_id"]:
                    raise ValueError("conflicting legacy agent ID")
                record["agent_id_legacy"] = record.pop("agent_id")
            if group == "snapshots":
                subject = record.get("subject") or {}
                if not isinstance(subject, dict) or subject.get("user_id", user) != user or subject.get("tenant_id", tenant) != tenant:
                    raise ValueError(f"{tenant}/{group}/{record_id}: subject conflict")
                record["subject"] = {"tenant_id": tenant, "user_id": user}
    migrated["deregister_migration_version"] = VERSION
    return migrated


def migrate_users(data):
    migrated = copy.deepcopy(data)
    if not isinstance(migrated.get("users", []), list):
        raise ValueError("users must be an array")
    for user in migrated.get("users", []):
        if not isinstance(user, dict):
            raise ValueError("invalid user record")
        user.pop("agent_subjects", None)
        user.pop("agent_identities", None)
    migrated["deregister_migration_version"] = VERSION
    return migrated


def verify_evidence(config, evidence, tenants):
    if config.agent_protocol_identity_mode != "tenant_user" or config.skills_delivery_mode != "pull":
        raise ValueError("apply requires tenant_user + pull")
    for field in ("writers_stopped", "clients_use_user_id", "full_release_cycle_observed", "strict_business_cycle_observed"):
        if evidence.get(field) is not True:
            raise ValueError(f"missing cutover evidence: {field}")
    if type(evidence.get("legacy_requests")) is not int or evidence["legacy_requests"] != 0:
        raise ValueError("observed legacy_requests must be 0")
    start = datetime.fromisoformat(evidence["observation_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(evidence["observation_end"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or not start < end <= datetime.now(timezone.utc):
        raise ValueError("invalid observation window")
    for field in ("skill_pull_isolation_verified", "replay_binding_verified_or_unused"):
        if not set(tenants).issubset(set(evidence.get(field, []))):
            raise ValueError(f"missing tenant evidence: {field}")


def migrate(config, *, tenants=None, apply=False, evidence=None, registry=None):
    registry = registry or TenantRegistry(config)
    known = sorted({tenant.tenant_id for tenant in registry.list_tenants()} | {"default"})
    selected = sorted(set(tenants or known))
    if not set(selected).issubset(known):
        raise ValueError("unknown tenant; cannot infer or substitute default")
    if not config.storage_pg_enabled and selected != ["default"]:
        raise ValueError("file mode supports only default")
    all_selected = selected == known
    if apply:
        verify_evidence(config, evidence or {}, known)
    manifest_key = f"deregister-{checksum(selected)[:16]}.manifest.json"
    # All sources and transformations are validated before any write.
    items = []
    for tenant in selected:
        for key in (AGENTS, CONTEXT):
            before = read(config, key, tenant)
            after = migrate_context(before, tenant) if key == CONTEXT else before
            items.append((tenant, key, before, after))
    before = read(config, USERS, "default")
    after = migrate_users(before) if all_selected else before
    items.append(("default", USERS, before, after))
    existing = read(config, manifest_key, "default")
    if existing:
        validate_manifest(config, existing)
        expected = {(item["tenant"], item["key"]): item for item in existing["objects"]}
        if set(expected) != {(tid, key) for tid, key, _, _ in items}:
            raise ValueError("manifest selection mismatch")
        for tid, key, before, after in items:
            entry = expected[tid, key]
            if checksum(before) not in {entry["before_sha256"], entry["after_sha256"]}:
                raise ValueError(f"source changed since backup: {tid}/{key}")
            if checksum(after) != entry["after_sha256"]:
                raise ValueError(f"migration output changed: {tid}/{key}")
        manifest = existing
    else:
        manifest = {"version": VERSION, "scope": scope(config), "tenants": selected,
                    "global_users_cleaned": all_selected, "manifest_key": manifest_key,
                    "evidence_sha256": checksum(evidence or {}), "objects": []}
        for tenant, key, before, after in items:
            backup_key = key.removesuffix(".json") + ".pre-deregister.json"
            backup = read(config, backup_key, tenant)
            original = before
            if backup and checksum(backup) != checksum(before):
                if key == CONTEXT and before.get("deregister_migration_version") == VERSION and migrate_context(backup, tenant) == after:
                    original = backup  # An earlier --tenant run completed this scope.
                else:
                    raise ValueError(f"immutable backup already exists: {tenant}/{backup_key}; use its manifest")
            manifest["objects"].append({"tenant": tenant, "key": key, "backup_key": backup_key,
                                        "before_count": counts(original), "after_count": counts(after),
                                        "before_sha256": checksum(original), "after_sha256": checksum(after)})
    if not apply:
        return {**manifest, "action": "dry-run"}
    # Backups and per-tenant manifests must be readable before any source update.
    for tenant, key, before, after in items:
        entry = next(e for e in manifest["objects"] if e["tenant"] == tenant and e["key"] == key)
        backup = read(config, entry["backup_key"], tenant)
        if checksum(backup) != entry["before_sha256"]:
            if checksum(before) != entry["before_sha256"]:
                raise ValueError("backup lost or corrupt; refusing to replace with migrated data")
            write(config, entry["backup_key"], tenant, before)
        elif not backup:
            write(config, entry["backup_key"], tenant, before)
    for tenant in selected:
        write(config, manifest_key, tenant, manifest)
    write(config, manifest_key, "default", manifest)
    for tenant, key, before, after in items:
        # Maintenance window plus optimistic verification: do not clobber a writer.
        if checksum(read(config, key, tenant)) != checksum(before):
            raise ValueError(f"concurrent write detected: {tenant}/{key}")
        if before != after:
            write(config, key, tenant, after)
    return {**manifest, "action": "applied"}


def validate_manifest(config, manifest):
    if manifest.get("version") != VERSION or manifest.get("scope") != scope(config):
        raise ValueError("manifest version/backend/scope mismatch")
    tenants = manifest.get("tenants")
    if not isinstance(tenants, list) or not tenants or len(tenants) != len(set(tenants)):
        raise ValueError("invalid manifest tenants")
    expected = {(tid, key) for tid in tenants for key in (AGENTS, CONTEXT)} | {("default", USERS)}
    seen = set()
    from teamEvolver.storage.pg_store import validate_tenant_id
    for entry in manifest.get("objects", []):
        tenant, key = entry["tenant"], entry["key"]
        validate_tenant_id(tenant)
        pair = (tenant, key)
        if pair not in expected or pair in seen or key not in NAMES:
            raise ValueError("invalid manifest object scope")
        if entry["backup_key"] != key.removesuffix(".json") + ".pre-deregister.json":
            raise ValueError("invalid backup key")
        seen.add(pair)
    if seen != expected:
        raise ValueError("incomplete manifest")


def restore(config, manifest, *, writers_stopped=False):
    if not writers_stopped:
        raise ValueError("restore requires --writers-stopped")
    validate_manifest(config, manifest)
    prepared = []
    for entry in manifest["objects"]:
        tenant, key = entry["tenant"], entry["key"]
        backup = read(config, entry["backup_key"], tenant)
        if checksum(backup) != entry["before_sha256"] or counts(backup) != entry["before_count"]:
            raise ValueError(f"backup checksum/count mismatch: {tenant}/{key}")
        current = read(config, key, tenant)
        if checksum(current) not in {entry["before_sha256"], entry["after_sha256"]}:
            raise ValueError(f"newer source data exists: {tenant}/{key}; reconcile offline before restore")
        prepared.append((tenant, key, backup, current))
    for tenant, key, backup, current in prepared:
        if checksum(read(config, key, tenant)) != checksum(current):
            raise ValueError(f"concurrent write detected: {tenant}/{key}")
        write(config, key, tenant, backup)
    return {**manifest, "action": "restored"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", type=Path, metavar="BACKUP_MANIFEST")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--tenant")
    selection.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        config = ConfigStore(args.config).to_config()
        if args.restore:
            result = restore(config, json.loads(args.restore.read_text()), writers_stopped=args.writers_stopped)
        else:
            if not args.tenant and not args.all_tenants:
                parser.error("choose --tenant or --all-tenants")
            result = migrate(config, tenants=[args.tenant] if args.tenant else None, apply=args.apply,
                             evidence=json.loads(args.evidence.read_text()) if args.evidence else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
