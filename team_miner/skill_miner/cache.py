"""Disposable local views of authoritative PG mining artifact snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .repository import identity
from .storage import PgMiningFileStore, file_path


def cache_root(services, tenant_id) -> Path:
    root = services.extras.get("skill_mining_cache_root")
    base = Path(root) if root else Path(tempfile.gettempdir()) / "teamEvolver-skill-mining"
    schema = hashlib.sha256(services.tx.pool.schema.encode()).hexdigest()[:16]
    tenant = hashlib.sha256(str(tenant_id).encode()).hexdigest()[:24]
    return base.resolve() / schema / tenant / uuid.uuid4().hex


def job_root(repository, job_id) -> Path:
    return Path(repository.project_root).resolve() / "mining_jobs" / identity(job_id)


def persist_cache(repository, job_id):
    """Durably checkpoint all working files before yielding or acknowledging work.

    Fails closed on a database failure or stale snapshot. A temporary directory
    is never accepted as a substitute for this successful PG checkpoint.
    """
    root = job_root(repository, job_id)
    def files():
        if not root.exists():
            return
        if root.is_symlink():
            raise ValueError("mining job cache cannot be a symbolic link")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("mining cache cannot contain symbolic links")
            if path.is_file():
                relative = file_path(path.relative_to(root).as_posix())
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                yield relative, size, digest.hexdigest(), path.read_bytes
    return PgMiningFileStore(repository.backend).checkpoint(job_id, files())


def restore_cache(repository, job_id):
    """Rebuild from PG on each new owner; remove files absent from the snapshot."""
    root = job_root(repository, job_id)
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".mining-restore-", dir=root.parent))
    retired = None
    try:
        def write(path, content):
            destination = stage / file_path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        summary = PgMiningFileStore(repository.backend).materialize(job_id, write)
        if root.exists() or root.is_symlink():
            retired = Path(tempfile.mkdtemp(prefix=".mining-retired-", dir=root.parent))
            retired.rmdir()
            os.replace(root, retired)
        os.replace(stage, root)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if retired is not None:
            if retired.is_symlink():
                retired.unlink(missing_ok=True)
            else:
                shutil.rmtree(retired, ignore_errors=True)
    return {**summary, "path": str(root)}
