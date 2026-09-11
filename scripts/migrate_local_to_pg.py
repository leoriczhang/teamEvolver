"""Migrate a local TeamEvolver store into PostgreSQL (multi-tenancy Phase 3).

Copies every object key from a ``LocalObjectStore`` root (default
``~/.teamEvolver/local_store/{hash8}/...``) into the ``default`` tenant of the
``teamevolver.objects`` table, preserving the exact key layout. Idempotent
(``INSERT ... ON CONFLICT DO UPDATE``): re-running with an unchanged source is
a no-op content-wise. Quiesce service writes during migration — upsert
overwrites, so a live writer racing the migration could lose newer PG data.

Usage:
    .venv/bin/python scripts/migrate_local_to_pg.py
    .venv/bin/python scripts/migrate_local_to_pg.py --dry-run
    .venv/bin/python scripts/migrate_local_to_pg.py --local-root ~/.teamEvolver/local_store/team-skill-evolver-abcd1234
    .venv/bin/python scripts/migrate_local_to_pg.py --tenant default --batch-size 200

The PG DSN/schema/pool settings come from the same ``OV_PG_*`` env vars and
``~/.teamEvolver/config.yaml`` ``storage_pg:`` section the service uses.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

TEAM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TEAM_ROOT))


from teamEvolver.storage.pg_store import (  # noqa: E402  (path inserted above)
    _BATCH_MAX_FILE_BYTES,
    _BATCH_MAX_OPERATIONS,
    _BATCH_MAX_TOTAL_BYTES,
)

# Keep headroom under the hard 16 MiB batch ceiling.
_BATCH_BYTE_BUDGET = _BATCH_MAX_TOTAL_BYTES - 1024 * 1024


def _resolve_local_root(config, override: str | None) -> str:
    if override:
        return override
    configured = str(getattr(config, "sharing_local_root", "") or "").strip()
    if configured:
        return configured
    # Default LocalObjectStore layout: ~/.teamEvolver/local_store/team-skill-evolver-{hash8}
    base = Path.home() / ".teamEvolver" / "local_store"
    if not base.is_dir():
        return str(base)
    candidates = sorted(base.glob("team-skill-evolver-*"))
    if not candidates:
        return str(base)
    # Prefer an existing populated store; fall back to the first match.
    for path in candidates:
        if any(path.rglob("*")):
            return str(path)
    return str(candidates[0])


def _pg_kwargs(config) -> dict:
    from teamEvolver.storage.pg_pool import dsn_from_env

    dsn = str(getattr(config, "storage_pg_dsn", "") or "") or dsn_from_env()
    if not dsn:
        raise SystemExit(
            "PostgreSQL DSN not configured: set OV_PG_* env vars or "
            "storage_pg.dsn in ~/.teamEvolver/config.yaml"
        )
    return dict(
        pg_dsn=dsn,
        pg_schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
        pg_pool_min=int(getattr(config, "storage_pg_pool_min", 2) or 2),
        pg_pool_max=int(getattr(config, "storage_pg_pool_max", 20) or 20),
        pg_command_timeout=float(
            getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-root",
        default=None,
        help="Local store root to migrate from (default: auto-detect).",
    )
    parser.add_argument(
        "--tenant",
        default="default",
        help="Target tenant_id in PG (default: default).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help=(
            "Max objects per batch_write transaction (default: 200; hard cap "
            f"{_BATCH_MAX_OPERATIONS}). Batches also split at "
            f"{_BATCH_BYTE_BUDGET // (1024 * 1024)} MiB cumulative."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List and count objects without writing to PG.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-migration content hash comparison.",
    )
    args = parser.parse_args()

    from teamEvolver.config_store import ConfigStore
    from teamEvolver.storage import build_object_store
    from teamEvolver.storage.local import LocalObjectStore
    from teamEvolver.storage.pg_store import validate_tenant_id

    tenant_id = validate_tenant_id(args.tenant)
    batch_size = max(1, min(int(args.batch_size), _BATCH_MAX_OPERATIONS))
    config = ConfigStore().to_config()
    local_root = _resolve_local_root(config, args.local_root)
    source = LocalObjectStore(local_root)

    print(f"[migrate] source: {local_root}", flush=True)
    print(f"[migrate] target: tenant={tenant_id}", flush=True)

    # Enumerate every key once.
    keys: list[str] = [info.key for info in source.iter_objects()]
    print(f"[migrate] discovered {len(keys)} objects", flush=True)

    if args.dry_run:
        total_bytes = 0
        for key in keys[:20]:
            try:
                size = len(source.get_object(key).read())
                total_bytes += size
            except Exception:  # noqa: BLE001
                size = -1
            print(f"  {key}  ({size} bytes)", flush=True)
        if len(keys) > 20:
            print(f"  ... and {len(keys) - 20} more", flush=True)
        print(f"[migrate] dry-run complete: {len(keys)} objects, no writes", flush=True)
        return 0

    pg = build_object_store(
        backend="postgres",
        tenant_id=tenant_id,
        **_pg_kwargs(config),
    )

    written = 0
    migrated_bytes = 0
    skipped_oversized: list[str] = []
    local_hashes: dict[str, str] = {}

    def _flush(chunk: dict[str, bytes]) -> None:
        nonlocal written
        if not chunk:
            return
        result = pg.batch_write(chunk, default_mode="upsert", wait=True)
        written += len(result.get("succeeded", chunk))
        print(
            f"[migrate] progress: {written}/{len(keys)} objects, "
            f"{migrated_bytes} bytes",
            flush=True,
        )

    chunk: dict[str, bytes] = {}
    chunk_bytes = 0
    for key in keys:
        try:
            content = source.get_object(key).read()
        except Exception:  # noqa: BLE001
            print(f"[migrate] WARN: failed to read {key}; skipping", flush=True)
            continue
        if len(content) > _BATCH_MAX_FILE_BYTES:
            skipped_oversized.append(key)
            print(
                f"[migrate] WARN: {key} is {len(content)} bytes "
                f"(> {_BATCH_MAX_FILE_BYTES}); skipping",
                flush=True,
            )
            continue
        if chunk and (len(chunk) >= batch_size or chunk_bytes + len(content) > _BATCH_BYTE_BUDGET):
            _flush(chunk)
            chunk, chunk_bytes = {}, 0
        chunk[key] = content
        chunk_bytes += len(content)
        migrated_bytes += len(content)
        local_hashes[key] = hashlib.sha256(content).hexdigest()
    _flush(chunk)

    if skipped_oversized:
        print(
            f"[migrate] WARN: {len(skipped_oversized)} oversized objects skipped",
            flush=True,
        )

    # Consistency check 1: key coverage (source minus skipped must exist in PG).
    pg_keys = {info.key for info in pg.iter_objects()}
    expected = set(local_hashes)
    missing = expected - pg_keys
    print(
        f"[migrate] key check: source={len(expected)} pg={len(pg_keys)} "
        f"written={written}",
        flush=True,
    )
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        print(
            f"[migrate] FAIL: {len(missing)} keys missing in PG (first: {sample})",
            flush=True,
        )
        return 2

    # Consistency check 2: content hash comparison (M4 acceptance).
    if not args.no_verify:
        mismatched: list[str] = []
        for idx, key in enumerate(sorted(expected), 1):
            try:
                remote = pg.get_object(key).read()
            except Exception:  # noqa: BLE001
                mismatched.append(key)
                continue
            if hashlib.sha256(remote).hexdigest() != local_hashes[key]:
                mismatched.append(key)
            if idx % 500 == 0:
                print(f"[migrate] verify: {idx}/{len(expected)} hashed", flush=True)
        if mismatched:
            sample = ", ".join(mismatched[:5])
            print(
                f"[migrate] FAIL: {len(mismatched)} hash mismatches (first: {sample})",
                flush=True,
            )
            return 2
        print(f"[migrate] hash verify passed ({len(expected)} objects)", flush=True)

    print("[migrate] consistency check passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
