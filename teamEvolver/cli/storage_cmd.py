"""Administrative object-store migration commands."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import click

from ..config_store import ConfigStore
from ..storage import PgObjectStore, is_not_found_error


CONTROL_PLANE_PREFIXES = (
    "skills/",
    "validation_jobs/",
    "candidate_skills/",
    "validation_results/",
    "validation_decisions/",
    "validation_evaluations/",
    "validation_claims/",
    "human_review/",
    "skill_version_context/",
    "skill_mutation_commits/",
    "skill_sync_outbox/",
    "skill_tombstones/",
)
CONTROL_PLANE_KEYS = frozenset(
    {
        "manifest.json",
        "evolve_skill_registry.json",
        "validation_open_jobs.json",
        "validation_decision_index.json",
    }
)
MAX_BATCH_OBJECTS = 200
MAX_BATCH_BYTES = 12 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_control_plane_key(key: str) -> bool:
    return key in CONTROL_PLANE_KEYS or key.startswith(CONTROL_PLANE_PREFIXES)


def _source_scope(root: Path, tenant_id: str) -> Path:
    tenant_root = root / "tenants" / tenant_id
    if tenant_id != "default" and tenant_root.is_dir():
        return tenant_root
    return root


def collect_control_plane_objects(
    source_roots: Iterable[str | Path],
    *,
    tenant_id: str,
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    """Merge local control-plane objects, reporting divergent duplicate keys."""
    objects: dict[str, bytes] = {}
    sources: dict[str, str] = {}
    conflicts: list[dict[str, str]] = []
    for raw_root in source_roots:
        root = Path(raw_root).expanduser().resolve()
        scope = _source_scope(root, tenant_id)
        if not scope.is_dir():
            raise ValueError(f"source root does not exist: {scope}")
        for path in sorted(scope.rglob("*")):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            key = path.relative_to(scope).as_posix()
            if not _is_control_plane_key(key):
                continue
            data = path.read_bytes()
            previous = objects.get(key)
            if previous is not None and previous != data:
                conflicts.append(
                    {
                        "key": key,
                        "first_source": sources[key],
                        "first_sha256": _sha256(previous),
                        "second_source": str(path),
                        "second_sha256": _sha256(data),
                    }
                )
                continue
            objects[key] = data
            sources.setdefault(key, str(path))
    return objects, conflicts


def _target_state(target: Any, objects: dict[str, bytes]) -> tuple[list[str], list[dict[str, str]]]:
    existing_same: list[str] = []
    conflicts: list[dict[str, str]] = []
    for key, data in sorted(objects.items()):
        try:
            current = target.get_object(key).read()
        except Exception as exc:
            if is_not_found_error(exc):
                continue
            raise
        if current == data:
            existing_same.append(key)
        else:
            conflicts.append(
                {
                    "key": key,
                    "source_sha256": _sha256(data),
                    "target_sha256": _sha256(current),
                }
            )
    return existing_same, conflicts


def _batches(objects: dict[str, bytes]) -> list[dict[str, bytes]]:
    batches: list[dict[str, bytes]] = []
    current: dict[str, bytes] = {}
    current_bytes = 0
    for key, data in sorted(objects.items()):
        size = len(data)
        if size > MAX_BATCH_BYTES:
            raise ValueError(f"object exceeds migration batch limit: {key}")
        if current and (
            len(current) >= MAX_BATCH_OBJECTS
            or current_bytes + size > MAX_BATCH_BYTES
        ):
            batches.append(current)
            current = {}
            current_bytes = 0
        current[key] = data
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def migrate_local_control_plane(
    source_roots: Iterable[str | Path],
    target: Any,
    *,
    tenant_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or apply a conflict-safe local-to-PostgreSQL control-plane copy."""
    roots = tuple(source_roots)
    objects, source_conflicts = collect_control_plane_objects(
        roots,
        tenant_id=tenant_id,
    )
    existing_same, target_conflicts = _target_state(target, objects)
    conflicts = [*source_conflicts, *target_conflicts]
    pending = {
        key: data for key, data in objects.items() if key not in set(existing_same)
    }
    plan = {
        "tenant_id": tenant_id,
        "source_objects": len(objects),
        "existing_same": len(existing_same),
        "pending": len(pending),
        "conflicts": conflicts,
        "applied": False,
        "batches": 0,
    }
    if conflicts or not apply:
        return plan
    if not bool(getattr(target, "native_batch_write", False)):
        raise ValueError("target must support transactional batch_write")

    batches = _batches(pending)
    for batch in batches:
        target.batch_write(
            batch,
            preconditions={
                key: {"kind": "create_if_absent"} for key in batch
            },
        )

    for key, expected in pending.items():
        actual = target.get_object(key).read()
        if actual != expected:
            raise RuntimeError(f"migration verification failed: {key}")

    migration_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    marker_key = f"admin_migrations/local-to-pg/{migration_id}.json"
    marker = {
        "migration_id": migration_id,
        "tenant_id": tenant_id,
        "source_roots": [str(Path(root).expanduser().resolve()) for root in roots],
        "object_count": len(objects),
        "copied_count": len(pending),
        "existing_same": len(existing_same),
        "batch_count": len(batches),
        "verified": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    target.batch_write(
        {marker_key: json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8")},
        preconditions={marker_key: {"kind": "create_if_absent"}},
    )
    return {
        **plan,
        "applied": True,
        "batches": len(batches),
        "migration_id": migration_id,
        "marker_key": marker_key,
    }


def _pg_target(config: Any, tenant_id: str) -> PgObjectStore:
    if not bool(getattr(config, "storage_pg_enabled", False)):
        raise click.ClickException("storage_pg.enabled=true is required")
    return PgObjectStore(
        dsn=str(getattr(config, "storage_pg_dsn", "") or ""),
        schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
        tenant_id=tenant_id,
        pool_min=int(getattr(config, "storage_pg_pool_min", 2) or 2),
        pool_max=int(getattr(config, "storage_pg_pool_max", 20) or 20),
        command_timeout=float(
            getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
        ),
        ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
    )


@click.group()
def storage() -> None:
    """Storage migration and maintenance commands."""


@storage.command(name="migrate-local-control-plane")
@click.option(
    "--source-root",
    "source_roots",
    multiple=True,
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
)
@click.option("--tenant-id", required=True)
@click.option("--apply/--dry-run", default=False)
def migrate_local_control_plane_cmd(
    source_roots: tuple[Path, ...],
    tenant_id: str,
    apply: bool,
) -> None:
    """Copy local Skill/validation control-plane objects into PostgreSQL."""
    target = _pg_target(ConfigStore().to_config(), tenant_id)
    try:
        result = migrate_local_control_plane(
            source_roots,
            target,
            tenant_id=tenant_id,
            apply=apply,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if result["conflicts"]:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise click.ClickException(
            f"migration stopped: {len(result['conflicts'])} conflict(s)"
        )
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
