"""Skill library repository.

PostgreSQL is the single authority for the tenant Skill library: the manifest
head (``skills``), the append-only version history (``skill_versions``), the
per-version file list (``skill_version_files``), content-addressed file bytes
(``skill_blobs``) and the mutation idempotency ledger (``skill_mutations``) —
all defined in ``003_skills.sql``. The local ``skills_dir`` is only a
rebuildable runtime cache materialised from these rows (see ``cache.py``).

Write discipline :

- Every mutation runs inside the caller's tenant transaction and first takes
 the skill head row lock (``SELECT... FOR UPDATE``), so concurrent mutations of
 one skill serialise across pods without a process-local lock.
- The mutation id is looked up *after* the lock: a repeated mutation id replays
 its first result (same version) instead of creating a second one; the same id
 with a different request is a :class:`SkillMutationConflict`.
- Version row, version files, blobs, head update, mutation and delivery outbox
 are written in that one transaction — no publish is visible without its
 durable delivery intent (``005_skill_delivery.sql``).
- Versions are append-only: rollback and delete add a new version (delete is a
 tombstone), history is never rewritten.

Bundles are validated before any write (SKILL.md wire format, path safety and
the tenant's ``skills`` bundle limits). Only this domain's tables are touched
.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from team_skills.config import BundleLimits
from team_skills.library import frontmatter
from team_skills.library.bundle import (
    bundle_file_records,
    bundle_tree_sha256,
    coerce_skill_bundle,
)
from team_skills.library.editor import validate_skill_name as _validate_skill_name
from teamEvolver.database.transaction import TransactionManager

_ENTRYPOINT = "SKILL.md"
MUTATION_ACTIONS = ("publish", "update", "rollback", "delete")


class SkillLibraryError(ValueError):
    """A request the library rejects deterministically (maps to HTTP 4xx)."""

    status_code = 400


class SkillNotFound(SkillLibraryError):
    status_code = 404


class SkillMutationConflict(SkillLibraryError):
    """A mutation id was reused for a different request."""

    status_code = 409


class SkillVersionConflict(SkillLibraryError):
    """The skill moved past the caller's expected base version."""

    status_code = 409


class SkillBundleTooLarge(SkillLibraryError):
    status_code = 413


@dataclass(frozen=True)
class SkillCommit:
    """Outcome of one library mutation."""

    name: str
    action: str
    status: str  # "published" | "unchanged"
    version: int
    previous_version: int
    mutation_id: str
    replayed: bool = False
    restored_from: int | None = None
    record: dict[str, Any] = field(default_factory=dict)

    def to_response(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "action": self.action,
            "status": self.status,
            "version": self.version,
            "previous_version": self.previous_version,
            "mutation_id": self.mutation_id,
            "replayed": self.replayed,
            **({"restored_from": self.restored_from} if self.restored_from is not None else {}),
        }


def skill_id_for(name: str) -> str:
    """Stable, name-derived skill id (same derivation the legacy registry used)."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def validate_skill_name(name: str) -> str:
    """Traversal-safe skill name, or :class:`SkillLibraryError` (HTTP 400)."""
    try:
        return _validate_skill_name(name)
    except ValueError as exc:
        raise SkillLibraryError(str(exc)) from exc


def _loads(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_bundle(
    name: str, bundle: Mapping[str, bytes | bytearray | str], limits: BundleLimits
) -> dict[str, bytes]:
    """Normalise and validate a bundle before it may be committed.

    - path safety + ignored-file filtering (``coerce_skill_bundle``);
    - ``SKILL.md`` present and in the strict single-frontmatter wire format whose
      ``name`` equals the skill name (no double frontmatter, no folded
      description — the format Agents consume);
    - the tenant's file-count / total / per-file byte limits.
    """
    try:
        clean = coerce_skill_bundle(bundle)
    except (TypeError, ValueError) as exc:
        raise SkillLibraryError(str(exc)) from exc
    if _ENTRYPOINT not in clean:
        raise SkillLibraryError("skill bundle requires SKILL.md")
    try:
        frontmatter.validate_skill_md(clean[_ENTRYPOINT], expected_name=name)
    except ValueError as exc:
        raise SkillLibraryError(str(exc)) from exc
    if len(clean) > limits.max_files:
        raise SkillBundleTooLarge(f"skill bundle exceeds {limits.max_files} files")
    total = 0
    for rel_path, data in clean.items():
        if len(data) > limits.max_file_bytes:
            raise SkillBundleTooLarge(f"{rel_path} exceeds {limits.max_file_bytes} bytes")
        total += len(data)
    if total > limits.max_bytes:
        raise SkillBundleTooLarge(f"skill bundle exceeds {limits.max_bytes} bytes")
    return clean


def bundle_metadata(name: str, bundle: Mapping[str, bytes]) -> dict[str, Any]:
    """Manifest metadata derived from a validated bundle."""
    entry: dict[str, Any] = {}
    frontmatter.enrich_manifest_entry_from_raw(entry, bundle[_ENTRYPOINT], name)
    return {
        "sha256": hashlib.sha256(bundle[_ENTRYPOINT]).hexdigest(),
        "tree_sha256": bundle_tree_sha256(bundle),
        "files": bundle_file_records(bundle),
        "description": str(entry.get("description") or ""),
        "category": str(entry.get("category") or "general"),
        "runtime_policy": dict(entry.get("runtime_policy") or {}),
    }


# --------------------------------------------------------------------------- #
# content-addressed blobs (shared with the candidate repository)
# --------------------------------------------------------------------------- #


async def put_blobs(conn: Any, schema: str, tenant_id: str, bundle: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """Insert each file's bytes once (dedupe by sha256); return file records."""
    records = bundle_file_records(bundle)
    by_path = dict(bundle)
    await conn.executemany(
        f"INSERT INTO {schema}.skill_blobs (tenant_id, sha256, size, content) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id, sha256) DO NOTHING",
        [(tenant_id, r["sha256"], int(r["size"]), by_path[str(r["path"])]) for r in records],
    )
    return records


def assemble_bundle(rows: list[Any]) -> dict[str, bytes]:
    """Rebuild a bundle from ``(path, sha256, content)`` rows, verifying hashes."""
    bundle: dict[str, bytes] = {}
    for row in rows:
        content = bytes(row["content"])
        if hashlib.sha256(content).hexdigest() != str(row["sha256"]):
            raise RuntimeError(f"skill blob hash mismatch for {row['path']}")
        bundle[str(row["path"])] = content
    return bundle


class SkillLibraryRepository:
    """Persistence for skill heads, versions, version files, blobs and mutations."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    # -- reads ------------------------------------------------------------ #

    _HEAD_COLUMNS = (
        "name, skill_id, current_version, deleted, description, category, sha256, "
        "tree_sha256, runtime_policy, updated_by, created_at, updated_at"
    )

    @staticmethod
    def _head(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["runtime_policy"] = _loads(item.get("runtime_policy"))
        item["version"] = int(item.pop("current_version") or 0)
        return item

    async def list_skills(self, conn: Any) -> list[dict[str, Any]]:
        """Live (non-deleted) skills with their current file records."""
        rows = await conn.fetch(
            f"SELECT {self._HEAD_COLUMNS} FROM {self._schema}.skills "
            "WHERE current_version > 0 AND NOT deleted ORDER BY name"
        )
        heads = [self._head(row) for row in rows]
        if not heads:
            return heads
        files = await conn.fetch(
            f"SELECT f.name, f.path, f.sha256, f.size FROM {self._schema}.skill_version_files f "
            f"JOIN {self._schema}.skills s ON s.tenant_id = f.tenant_id AND s.name = f.name "
            "  AND s.current_version = f.version "
            "WHERE NOT s.deleted ORDER BY f.name, f.path"
        )
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in files:
            by_name.setdefault(str(row["name"]), []).append(
                {"path": str(row["path"]), "sha256": str(row["sha256"]), "size": int(row["size"])}
            )
        for head in heads:
            head["files"] = by_name.get(head["name"], [])
        return heads

    async def get_skill(self, conn: Any, name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"SELECT {self._HEAD_COLUMNS} FROM {self._schema}.skills WHERE name = $1", name
        )
        if row is None or int(row["current_version"] or 0) <= 0:
            return None
        head = self._head(row)
        if head["deleted"] and not include_deleted:
            return None
        return head

    async def list_versions(self, conn: Any, name: str) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            "SELECT version, action, deleted, sha256, tree_sha256, file_count, total_bytes, "
            "  description, category, restored_from, mutation_id, actor, created_at "
            f"FROM {self._schema}.skill_versions WHERE name = $1 ORDER BY version DESC",
            name,
        )
        return [dict(row) for row in rows]

    async def get_version(self, conn: Any, name: str, version: int) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            "SELECT version, action, deleted, sha256, tree_sha256, file_count, total_bytes, "
            "  description, category, runtime_policy, restored_from, mutation_id, actor, created_at "
            f"FROM {self._schema}.skill_versions WHERE name = $1 AND version = $2",
            name,
            int(version),
        )
        if row is None:
            return None
        item = dict(row)
        item["runtime_policy"] = _loads(item.get("runtime_policy"))
        return item

    async def read_version_bundle(self, conn: Any, name: str, version: int) -> dict[str, bytes]:
        """The immutable bundle of one non-tombstone version."""
        meta = await self.get_version(conn, name, version)
        if meta is None or meta["deleted"]:
            raise SkillNotFound(f"version v{int(version)} of {name} not found")
        rows = await conn.fetch(
            f"SELECT f.path, f.sha256, b.content FROM {self._schema}.skill_version_files f "
            f"JOIN {self._schema}.skill_blobs b ON b.tenant_id = f.tenant_id AND b.sha256 = f.sha256 "
            "WHERE f.name = $1 AND f.version = $2 ORDER BY f.path",
            name,
            int(version),
        )
        bundle = assemble_bundle(rows)
        if bundle_tree_sha256(bundle) != str(meta["tree_sha256"]):
            raise RuntimeError(f"skill {name} v{int(version)} tree hash mismatch")
        return bundle

    async def read_current_bundle(self, conn: Any, name: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        head = await self.get_skill(conn, name)
        if head is None:
            raise SkillNotFound(f"skill not found: {name}")
        return head, await self.read_version_bundle(conn, name, head["version"])

    async def read_current_bundles(self, conn: Any) -> dict[str, dict[str, bytes]]:
        """Every live skill's current bundle in one query (Agent pull, cache rebuild)."""
        rows = await conn.fetch(
            f"SELECT f.name, f.path, f.sha256, b.content FROM {self._schema}.skill_version_files f "
            f"JOIN {self._schema}.skills s ON s.tenant_id = f.tenant_id AND s.name = f.name "
            "  AND s.current_version = f.version "
            f"JOIN {self._schema}.skill_blobs b ON b.tenant_id = f.tenant_id AND b.sha256 = f.sha256 "
            "WHERE NOT s.deleted ORDER BY f.name, f.path"
        )
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(str(row["name"]), []).append(row)
        return {name: assemble_bundle(items) for name, items in grouped.items()}

    async def get_mutation(self, conn: Any, mutation_id: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            "SELECT mutation_id, action, name, status, version, previous_version, request_hash, "
            f"  actor, source, metadata, created_at FROM {self._schema}.skill_mutations "
            "WHERE mutation_id = $1",
            mutation_id,
        )
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _loads(item.get("metadata"))
        return item

    # -- write helpers ----------------------------------------------------- #

    async def _lock_head(self, conn: Any, name: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"SELECT name, current_version, deleted, tree_sha256 FROM {self._schema}.skills "
            "WHERE name = $1 FOR UPDATE",
            name,
        )
        return dict(row) if row is not None else None

    async def _replay(
        self, conn: Any, mutation_id: str, *, name: str, request_hash: str
    ) -> SkillCommit | None:
        existing = await self.get_mutation(conn, mutation_id)
        if existing is None:
            return None
        if existing["name"] != name or existing["request_hash"] != request_hash:
            raise SkillMutationConflict(f"mutation id {mutation_id!r} was already used for a different request")
        head = await self.get_skill(conn, name, include_deleted=True) or {}
        return SkillCommit(
            name=name,
            action=str(existing["action"]),
            status=str(existing["status"]),
            version=int(existing["version"]),
            previous_version=int(existing["previous_version"]),
            mutation_id=mutation_id,
            replayed=True,
            record=head,
        )

    async def _record_mutation(
        self,
        conn: Any,
        *,
        tenant_id: str,
        mutation_id: str,
        action: str,
        name: str,
        status: str,
        version: int,
        previous_version: int,
        request_hash: str,
        actor: str,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        try:
            await conn.execute(
                f"INSERT INTO {self._schema}.skill_mutations "
                "(tenant_id, mutation_id, action, name, status, version, previous_version, "
                " request_hash, actor, source, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)",
                tenant_id,
                mutation_id,
                action,
                name,
                status,
                int(version),
                int(previous_version),
                request_hash,
                actor,
                source,
                json.dumps(metadata or {}),
            )
        except Exception as exc:
            # The same id raced in for a *different* skill (each holds its own
            # head lock, so both passed the replay check). Unique violation =
            # SQLSTATE 23505; the transaction rolls back, nothing is published.
            if getattr(exc, "sqlstate", "") == "23505":
                raise SkillMutationConflict(f"mutation id {mutation_id!r} is already in use") from exc
            raise

    async def _append_version(
        self,
        conn: Any,
        *,
        tenant_id: str,
        name: str,
        version: int,
        action: str,
        bundle: dict[str, bytes] | None,
        meta: dict[str, Any],
        mutation_id: str,
        actor: str,
        restored_from: int | None = None,
    ) -> None:
        """Insert one version row (+ blobs + files) and move the head to it."""
        deleted = bundle is None
        records = [] if deleted else await put_blobs(conn, self._schema, tenant_id, bundle)
        await conn.execute(
            f"INSERT INTO {self._schema}.skill_versions "
            "(tenant_id, name, version, action, deleted, sha256, tree_sha256, file_count, "
            " total_bytes, description, category, runtime_policy, restored_from, mutation_id, actor) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15)",
            tenant_id,
            name,
            int(version),
            action,
            deleted,
            str(meta.get("sha256") or ""),
            str(meta.get("tree_sha256") or ""),
            len(records),
            sum(int(r["size"]) for r in records),
            str(meta.get("description") or ""),
            str(meta.get("category") or "general"),
            json.dumps(meta.get("runtime_policy") or {}),
            restored_from,
            mutation_id,
            actor,
        )
        if records:
            await conn.executemany(
                f"INSERT INTO {self._schema}.skill_version_files "
                "(tenant_id, name, version, path, sha256, size) VALUES ($1, $2, $3, $4, $5, $6)",
                [(tenant_id, name, int(version), r["path"], r["sha256"], int(r["size"])) for r in records],
            )
        await conn.execute(
            f"UPDATE {self._schema}.skills SET current_version = $2, deleted = $3, "
            "  description = $4, category = $5, sha256 = $6, tree_sha256 = $7, "
            "  runtime_policy = $8::jsonb, updated_by = $9, updated_at = now() "
            "WHERE name = $1",
            name,
            int(version),
            deleted,
            str(meta.get("description") or ""),
            str(meta.get("category") or "general"),
            str(meta.get("sha256") or ""),
            str(meta.get("tree_sha256") or ""),
            json.dumps(meta.get("runtime_policy") or {}),
            actor,
        )

    # -- mutations (each runs in the caller's tenant transaction) --------- #

    async def commit_bundle(
        self,
        conn: Any,
        *,
        tenant_id: str,
        name: str,
        bundle: Mapping[str, bytes | bytearray | str],
        mutation_id: str,
        limits: BundleLimits,
        actor: str = "",
        source: str = "console",
        metadata: dict[str, Any] | None = None,
        base_version: int | None = None,
    ) -> SkillCommit:
        """Publish ``bundle`` as the skill's new current version.

        ``publish`` when the skill is new (or was deleted), ``update`` otherwise.
        An identical tree is recorded as ``unchanged`` (no new version). When
        ``base_version`` is given the commit only proceeds if the skill is still
        at that version (optimistic concurrency for read-modify-write callers).
        """
        name = validate_skill_name(name)
        clean = validate_bundle(name, bundle, limits)
        meta = bundle_metadata(name, clean)
        request_hash = _request_hash({"op": "bundle", "name": name, "tree": meta["tree_sha256"]})

        await conn.execute(
            f"INSERT INTO {self._schema}.skills (tenant_id, name, skill_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (tenant_id, name) DO NOTHING",
            tenant_id,
            name,
            skill_id_for(name),
        )
        head = await self._lock_head(conn, name)
        replay = await self._replay(conn, mutation_id, name=name, request_hash=request_hash)
        if replay is not None:
            return replay
        current = int(head["current_version"] or 0)
        live = current > 0 and not head["deleted"]
        if base_version is not None and int(base_version) != current:
            raise SkillVersionConflict(f"{name} is at v{current}, not the expected v{int(base_version)}")
        action = "update" if live else "publish"
        if live and str(head["tree_sha256"]) == meta["tree_sha256"]:
            await self._record_mutation(
                conn, tenant_id=tenant_id, mutation_id=mutation_id, action=action, name=name,
                status="unchanged", version=current, previous_version=current,
                request_hash=request_hash, actor=actor, source=source, metadata=metadata,
            )
            return SkillCommit(name, action, "unchanged", current, current, mutation_id,
                               record=await self.get_skill(conn, name) or {})
        version = current + 1
        await self._append_version(
            conn, tenant_id=tenant_id, name=name, version=version, action=action, bundle=clean,
            meta=meta, mutation_id=mutation_id, actor=actor,
        )
        await self._record_mutation(
            conn, tenant_id=tenant_id, mutation_id=mutation_id, action=action, name=name,
            status="published", version=version, previous_version=current,
            request_hash=request_hash, actor=actor, source=source, metadata=metadata,
        )
        return SkillCommit(name, action, "published", version, current, mutation_id,
                           record=await self.get_skill(conn, name) or {})

    async def commit_rollback(
        self,
        conn: Any,
        *,
        tenant_id: str,
        name: str,
        target_version: int,
        mutation_id: str,
        actor: str = "",
        source: str = "console",
        metadata: dict[str, Any] | None = None,
    ) -> SkillCommit:
        """Republish ``target_version``'s bundle as a new version (history kept)."""
        name = validate_skill_name(name)
        target = int(target_version)
        request_hash = _request_hash({"op": "rollback", "name": name, "target": target})
        head = await self._lock_head(conn, name)
        if head is None or int(head["current_version"] or 0) <= 0:
            raise SkillNotFound(f"skill not found: {name}")
        replay = await self._replay(conn, mutation_id, name=name, request_hash=request_hash)
        if replay is not None:
            return replay
        if head["deleted"]:
            raise SkillNotFound(f"skill not found: {name}")
        bundle = await self.read_version_bundle(conn, name, target)
        meta = bundle_metadata(name, bundle)
        current = int(head["current_version"])
        version = current + 1
        await self._append_version(
            conn, tenant_id=tenant_id, name=name, version=version, action="rollback", bundle=bundle,
            meta=meta, mutation_id=mutation_id, actor=actor, restored_from=target,
        )
        await self._record_mutation(
            conn, tenant_id=tenant_id, mutation_id=mutation_id, action="rollback", name=name,
            status="published", version=version, previous_version=current,
            request_hash=request_hash, actor=actor, source=source,
            metadata={**(metadata or {}), "restored_from": target},
        )
        return SkillCommit(name, "rollback", "published", version, current, mutation_id,
                           restored_from=target, record=await self.get_skill(conn, name) or {})

    async def commit_delete(
        self,
        conn: Any,
        *,
        tenant_id: str,
        name: str,
        mutation_id: str,
        actor: str = "",
        source: str = "console",
        metadata: dict[str, Any] | None = None,
    ) -> SkillCommit:
        """Record a delete tombstone version; the skill leaves the live manifest."""
        name = validate_skill_name(name)
        request_hash = _request_hash({"op": "delete", "name": name})
        head = await self._lock_head(conn, name)
        if head is None or int(head["current_version"] or 0) <= 0:
            raise SkillNotFound(f"skill not found: {name}")
        replay = await self._replay(conn, mutation_id, name=name, request_hash=request_hash)
        if replay is not None:
            return replay
        if head["deleted"]:
            raise SkillNotFound(f"skill not found: {name}")
        current = int(head["current_version"])
        version = current + 1
        await self._append_version(
            conn, tenant_id=tenant_id, name=name, version=version, action="delete", bundle=None,
            meta={"tree_sha256": str(head["tree_sha256"] or "")}, mutation_id=mutation_id, actor=actor,
        )
        await self._record_mutation(
            conn, tenant_id=tenant_id, mutation_id=mutation_id, action="delete", name=name,
            status="published", version=version, previous_version=current,
            request_hash=request_hash, actor=actor, source=source, metadata=metadata,
        )
        return SkillCommit(name, "delete", "published", version, current, mutation_id,
                           record=await self.get_skill(conn, name, include_deleted=True) or {})


__all__ = [
    "MUTATION_ACTIONS",
    "SkillLibraryError",
    "SkillNotFound",
    "SkillMutationConflict",
    "SkillVersionConflict",
    "SkillBundleTooLarge",
    "SkillCommit",
    "SkillLibraryRepository",
    "assemble_bundle",
    "bundle_metadata",
    "put_blobs",
    "skill_id_for",
    "validate_bundle",
    "validate_skill_name",
]
