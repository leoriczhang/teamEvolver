"""PostgreSQL artifact snapshots, independent of disposable filesystem caches."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping

from .repository import MiningRevisionConflict, identity


def file_path(value) -> str:
    path = str(value)
    if (not path or "\\" in path or ":" in path or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("unsafe mining artifact path")
    return path


def validate_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    result = {}
    for name, content in files.items():
        path = file_path(name)
        if not isinstance(content, bytes):
            raise TypeError("mining artifact content must be bytes")
        result[path] = content
    return result


class PgMiningFileStore:
    def __init__(self, backend):
        self.backend = backend

    def read_file(self, job_id, path, *, maximum):
        backend, name, path = self.backend, identity(job_id), file_path(path)

        async def read(conn):
            row = await conn.fetchrow(
                f"SELECT f.size,f.sha256,CASE WHEN f.size<=$4 THEN f.content ELSE NULL END AS content "
                f"FROM {backend.schema}.skill_mining_files f JOIN {backend.schema}.skill_mining_jobs j "
                "USING(tenant_id,job_id) WHERE f.tenant_id=$1 AND f.job_id=$2 AND f.path=$3 AND NOT j.deleted",
                backend.tenant_id, name, path, maximum,
            )
            if row is None:
                raise FileNotFoundError(path)
            if row["size"] > maximum:
                raise ValueError("产物超过大小限制")
            content = bytes(row["content"])
            if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError("mining artifact content hash mismatch")
            return content
        return backend._run(read)

    def materialize(self, job_id, write_file):
        """Restore one file at a time with a server cursor and a stable snapshot."""
        backend, name = self.backend, identity(job_id)

        async def read(conn):
            revision = await conn.fetchval(
                f"SELECT files_revision FROM {backend.schema}.skill_mining_jobs "
                "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted FOR SHARE", backend.tenant_id, name,
            )
            if revision is None:
                raise KeyError(name)
            rows = conn.cursor(
                f"SELECT path,sha256,size,content FROM {backend.schema}.skill_mining_files "
                "WHERE tenant_id=$1 AND job_id=$2 ORDER BY path", backend.tenant_id, name, prefetch=1,
            )
            count, total = 0, 0
            async for row in rows:
                content = bytes(row["content"])
                if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                    raise ValueError("mining artifact content hash mismatch")
                await asyncio.to_thread(write_file, file_path(row["path"]), content)
                count += 1
                total += len(content)
            return int(revision), {"files": count, "bytes": total}

        with backend._lock:
            revision, summary = backend._run(read, timeout=None)
            backend._file_revisions[name] = revision
        return summary

    def load(self, job_id) -> dict[str, bytes]:
        """Convenience bundle read; cache restoration uses streaming materialize."""
        files = {}
        self.materialize(job_id, lambda path, content: files.__setitem__(path, content))
        return files

    def save(self, job_id, files: Mapping[str, bytes]):
        clean = validate_files(files)
        return self.checkpoint(job_id, (
            (path, len(data), hashlib.sha256(data).hexdigest(), lambda data=data: data)
            for path, data in clean.items()
        ))

    def checkpoint(self, job_id, file_records):
        """Atomically replace the file index while transferring only changed files.

        Records yield (path, size, sha256, read_bytes). Only one changed file is
        loaded at a time. Source/round/output limits remain owned by the core;
        archival copies do not introduce a smaller aggregate storage budget.
        """
        backend, name = self.backend, identity(job_id)
        with backend._lock:
            if name not in backend._file_revisions:
                raise MiningRevisionConflict("load job or artifact snapshot before saving artifacts")
            expected = backend._file_revisions[name]

            async def write(conn):
                await backend.guard(conn)
                revision = await conn.fetchval(
                    f"UPDATE {backend.schema}.skill_mining_jobs SET files_revision=files_revision+1,updated_at=now() "
                    "WHERE tenant_id=$1 AND job_id=$2 AND NOT deleted AND files_revision=$3 "
                    "RETURNING files_revision", backend.tenant_id, name, expected,
                )
                if revision is None:
                    raise MiningRevisionConflict("mining artifacts changed; restore before saving")
                prior = {
                    str(row["path"]): (int(row["size"]), str(row["sha256"]))
                    for row in await conn.fetch(
                        f"SELECT path,size,sha256 FROM {backend.schema}.skill_mining_files "
                        "WHERE tenant_id=$1 AND job_id=$2", backend.tenant_id, name,
                    )
                }
                seen, total = set(), 0
                records, finished = iter(file_records), object()
                while True:
                    record = await asyncio.to_thread(next, records, finished)
                    if record is finished:
                        break
                    raw_path, size, digest, read_bytes = record
                    path = file_path(raw_path)
                    if path in seen:
                        raise ValueError("duplicate mining artifact path")
                    seen.add(path)
                    total += size
                    if prior.get(path) == (size, digest):
                        continue
                    data = await asyncio.to_thread(read_bytes)
                    if not isinstance(data, bytes) or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError("mining artifact changed during checkpoint")
                    await backend.guard(conn)
                    await conn.execute(
                        f"INSERT INTO {backend.schema}.skill_mining_files (tenant_id,job_id,path,sha256,size,content) "
                        "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (tenant_id,job_id,path) DO UPDATE "
                        "SET sha256=EXCLUDED.sha256,size=EXCLUDED.size,content=EXCLUDED.content,updated_at=now()",
                        backend.tenant_id, name, path, digest, size, data,
                    )
                    del data
                await conn.execute(
                    f"DELETE FROM {backend.schema}.skill_mining_files WHERE tenant_id=$1 AND job_id=$2 "
                    "AND NOT (path=ANY($3::text[]))", backend.tenant_id, name, list(seen),
                )
                return int(revision), {"files": len(seen), "bytes": total}

            revision, summary = backend._run(write, timeout=None)
            backend._file_revisions[name] = revision
        return summary
