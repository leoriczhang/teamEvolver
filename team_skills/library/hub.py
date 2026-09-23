"""Skill Hub: durable Skill sync via pluggable object storage.

A bidirectional sync between a working Skill directory and local/NAS or
OpenViking object storage, with incremental (sha256-based) transfers. Default
pull mirrors the durable snapshot into the working directory with backup and
rollback safety.

Usage::

    hub = SkillHub.team_from_config(config)
    hub.pull_skills("/path/to/local/skills")   # mirror durable snapshot locally
    hub.push_skills("/path/to/local/skills")   # upload new/updated skills
    hub.list_remote()                          # list stored skills
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Collection, Optional

if TYPE_CHECKING:
    from team_skills.library.mirror import VikingSkillMirror

from team_skills.library import frontmatter, layout
from team_skills.library.bundle import (
    bundle_entrypoint_bytes,
    bundle_file_records,
    bundle_has_only_entrypoint,
    bundle_tree_sha256,
    coerce_skill_bundle,
    is_ignored_bundle_rel_path,
    read_skill_bundle_with_meta,
    write_skill_bundle,
)
from team_skills.library.publication import (
    load_manifest_snapshot,
    publish_skill_bundle_batch,
)
from team_skills.library.registry import SkillIDRegistry
from teamEvolver.storage import (
    _VIKING_ROOT_PREFIX,
    InMemoryObjectStore,
    LocalObjectStore,
    PgObjectStore,
    build_object_store,
    is_not_found_error,
    normalize_backend,
    peer_key_prefix,
    pg_dsn_available,
)

logger = logging.getLogger(__name__)

# Local-only scratch directory names, kept out of any skills root.
_BACKUP_DIRNAME = ".teamEvolver_backups"
_STAGE_PREFIX = ".teamEvolver_pull_stage_"
# Serialises hub-only bundle read-modify-write (console workbench saves) so two
# concurrent edits to one skill cannot silently drop each other's files.
_BUNDLE_WRITE_LOCK = threading.RLock()


class SkillFileConflictError(RuntimeError):
    """Raised when a skill file changed in storage since the caller read it."""


class SkillHub:
    """Sync skills between a local directory and a shared object store."""

    def __init__(
        self,
        *,
        backend: str,
        endpoint: str,
        local_root: str = "",
        customer_id: str = "",
        user_alias: str = "",
        viking_endpoint: str = "",
        viking_api_key: str = "",
        viking_account: str = "",
        viking_user: str = "",
        viking_agent: str = "",
        viking_agent_id: str = "",
        viking_root_prefix: str = "",
        viking_group_id: str = "",
        viking_namespace: str = "resources",
        allow_fallback: bool = False,
        fallback_root: str = "",
        fallback_backend: str = "local",
        mirror_spool_dir: str = "",
        # PostgreSQL local-state backend (used when backend == "postgres").
        # Empty pg_dsn derives from the OV_PG_* environment variables.
        pg_dsn: str = "",
        pg_schema: str = "teamevolver",
        pg_pool_min: int = 2,
        pg_pool_max: int = 20,
        pg_command_timeout: float = 30.0,
        pg_ssl: str = "prefer",
        tenant_id: str = "default",
    ):
        # ``local_root`` selects the root of the built-in local backend and
        # doubles as an alias for ``fallback_root``: when ``allow_fallback``
        # is set and the configured OpenViking endpoint is unavailable, the
        # bucket becomes teamEvolver's own filesystem store.
        effective_endpoint = endpoint or viking_endpoint
        self._bucket = build_object_store(
            backend=backend,
            endpoint=effective_endpoint,
            local_root=local_root,
            viking_account=viking_account,
            viking_user=viking_user,
            viking_agent=viking_agent,
            viking_api_key=viking_api_key,
            viking_agent_id=viking_agent_id,
            viking_root_prefix=viking_root_prefix,
            viking_group_id=viking_group_id,
            viking_namespace=viking_namespace,
            allow_fallback=allow_fallback,
            fallback_root=fallback_root,
            fallback_backend=fallback_backend,
            pg_dsn=pg_dsn,
            pg_schema=pg_schema,
            pg_pool_min=pg_pool_min,
            pg_pool_max=pg_pool_max,
            pg_command_timeout=pg_command_timeout,
            pg_ssl=pg_ssl,
            tenant_id=tenant_id,
        )
        # Per-customer (peer) scope for isolated artifacts. Empty = agent level.
        self._customer_id = str(customer_id or "").strip("/")
        self._user_alias = user_alias or os.environ.get("USER", "anonymous")
        # Tenant scope: non-default tenants get a key prefix so their skills,
        # manifest and registry are isolated from other tenants in the same
        # object store.  PgObjectStore also uses tenant_id for row-level
        # isolation, but the key prefix is still needed for local/viking
        # backends and for the in-memory SkillIDRegistry.
        self._tenant_id = str(tenant_id or "default").strip()
        # Optional mirror target attached by _build when the primary bucket is
        # local: the hub used by the mirror outbox to push the skills subtree
        # to OpenViking for remote Agents. None when mirroring is disabled or
        # the primary bucket is already viking.
        self._mirror_viking_hub: Optional["SkillHub"] = None
        # Spool directory for mirror deliveries (empty = default
        # ~/.teamEvolver/skill_mirror_spool). Kept on the hub so every enqueue
        # writes to the same spool the flusher/status read from.
        self._mirror_spool_dir = str(mirror_spool_dir or "")

    @property
    def mirror_viking_hub(self) -> Optional["SkillHub"]:
        """Viking hub used by the skill mirror outbox (None when not mirroring)."""
        return self._mirror_viking_hub

    @property
    def _is_pg_backend(self) -> bool:
        """True when the primary bucket is the PostgreSQL object store.

        PG enforces tenant isolation via Row-Level Security, so skill keys
        stay bare (no ``tenants/<id>/`` directory prefix).
        """
        return isinstance(self._bucket, PgObjectStore)

    def _build_mirror(self) -> Optional["VikingSkillMirror"]:
        """Build the mirror outbox for this hub (None when not mirroring)."""
        if self._mirror_viking_hub is None:
            return None
        from team_skills.library.mirror import VikingSkillMirror

        return VikingSkillMirror(
            spool_dir=self._mirror_spool_dir or None,
            viking_hub=self._mirror_viking_hub,
            sequence_bucket=self._bucket,
        )

    def mirror_enqueue_push(self, skill_name: str, bundle_files: dict[str, bytes]) -> None:
        """Enqueue an OpenViking mirror for one freshly-published skill.

        No-op unless this hub is local-backed with a mirror attached. Never
        raises — mirroring must not break the publish path; failures are
        retried by the background flusher.
        """
        mirror = self._build_mirror()
        if mirror is None:
            return
        try:
            mirror.enqueue_skill(skill_name, bundle_files)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillHub] mirror enqueue failed for %s: %s", skill_name, exc)

    def mirror_enqueue_delete(self, skill_name: str) -> None:
        """Enqueue removal of one skill's mirrored subtree (best-effort)."""
        mirror = self._build_mirror()
        if mirror is None:
            return
        try:
            mirror.enqueue_delete(skill_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillHub] mirror delete enqueue failed for %s: %s", skill_name, exc)

    @classmethod
    def from_bucket(
        cls,
        bucket,
        *,
        customer_id: str = "",
        user_alias: str = "",
        tenant_id: str = "default",
    ) -> "SkillHub":
        """Build a hub around an already-constructed object store.

        Bypasses backend/endpoint resolution — useful for tests and mock-mode
        callers that supply an in-process store directly.
        """
        hub = cls.__new__(cls)
        hub._bucket = bucket
        hub._customer_id = str(customer_id or "").strip("/")
        hub._user_alias = user_alias or os.environ.get("USER", "anonymous")
        hub._tenant_id = str(tenant_id or "default").strip()
        hub._mirror_viking_hub = None
        hub._mirror_spool_dir = ""
        return hub

    # ------------------------------------------------------------------ #
    # Config-driven constructors                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    def _build(
        cls,
        config,
        *,
        backend_field: str,
        customer_id: str,
        namespace: str,
        key_scope: str = "team",
        allow_none: bool = False,
        tenant_id: str = "default",
    ) -> Optional["SkillHub"]:
        """Shared builder for the three config-driven constructors.

        *backend_field* selects the per-purpose backend key; *allow_none*
        returns ``None`` (rather than an empty-backend hub) when nothing is
        configured, matching the object-storage variant.

        Per-purpose split: ``sharing_session_backend`` / ``sharing_skill_backend``
        default to the built-in local backend (empty → "local"); OpenViking is
        kept only as the cross-machine mirror target for the team skill
        subtree. teamEvolver's own ledgers (registry / manifest / indexes /
        validation / session queue) therefore stay on the local store while
        remote Agents still read skills from ``viking://resources/...``.
        """
        purpose_backend = str(getattr(config, backend_field, "") or "").strip().lower()
        viking_endpoint = str(getattr(config, "sharing_viking_endpoint", "") or "")
        # Per-purpose split: an explicit per-purpose backend always wins; an
        # empty value resolves to the built-in local backend, matching the
        # TeamEvolverConfig contract ("Empty means 'local' for each"). A remote
        # Agent that needs to read the mirrored library from OpenViking sets
        # sharing_skill_backend="viking" explicitly.
        #
        # The previous sharing_backend=="viking" inheritance is removed: it
        # contradicted the contract because sharing_local_root="" means "use the
        # default root", not "no local store", so treating its emptiness as a
        # remote-Agent signal mis-routed teamEvolver's own service to viking
        # — candidates written locally were invisible to the read-side UI.
        if purpose_backend:
            backend = purpose_backend
        else:
            backend = "local"
        endpoint = str(getattr(config, "sharing_endpoint", "") or "")
        legacy_viking_api_key = str(getattr(config, "sharing_viking_api_key", "") or "")
        personal_viking_api_key = str(getattr(config, "sharing_viking_personal_api_key", "") or "")
        team_viking_api_key = str(getattr(config, "sharing_viking_team_api_key", "") or "")
        if key_scope == "personal":
            viking_api_key = personal_viking_api_key or legacy_viking_api_key
        else:
            viking_api_key = team_viking_api_key or legacy_viking_api_key
        has_viking_key = bool(viking_api_key)
        sharing_enabled = bool(getattr(config, "sharing_enabled", False))

        if allow_none:
            resolved = normalize_backend(backend, endpoint=viking_endpoint)
            if not resolved:
                # No per-purpose backend and no explicit request for local: fall
                # back to viking when an endpoint exists (legacy configs that
                # predate the local backend), otherwise bail out.
                if viking_endpoint and (sharing_enabled or has_viking_key):
                    resolved = "viking"
                elif sharing_enabled:
                    resolved = "local"
            if not resolved:
                return None
            backend = resolved
        else:
            backend = normalize_backend(backend) or "local"

        allow_fallback = bool(getattr(config, "sharing_local_fallback_enabled", True))
        shared_local_root = str(getattr(config, "sharing_local_root", "") or "")
        local_root = (
            str(getattr(config, "sharing_skill_local_root", "") or "")
            if backend_field == "sharing_skill_backend"
            else shared_local_root
        ) or shared_local_root
        viking_kwargs = dict(
            viking_endpoint=viking_endpoint,
            viking_api_key=viking_api_key,
            viking_account=str(getattr(config, "sharing_viking_account", "") or "default"),
            viking_user=str(getattr(config, "sharing_viking_user", "") or "team"),
            viking_agent=str(getattr(config, "sharing_viking_agent", "") or _VIKING_ROOT_PREFIX),
            viking_agent_id=str(getattr(config, "sharing_viking_agent_id", "") or ""),
            viking_root_prefix=str(getattr(config, "sharing_viking_root_prefix", "") or _VIKING_ROOT_PREFIX),
            viking_group_id=str(getattr(config, "sharing_viking_group_id", "") or ""),
        )
        # PostgreSQL local-state backend options (used when the resolved backend
        # is "postgres"); ignored by the other backends.
        pg_kwargs = dict(
            pg_dsn=str(getattr(config, "storage_pg_dsn", "") or ""),
            pg_schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
            pg_pool_min=int(getattr(config, "storage_pg_pool_min", 2) or 2),
            pg_pool_max=int(getattr(config, "storage_pg_pool_max", 20) or 20),
            pg_command_timeout=float(
                getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
            ),
            pg_ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
        )
        # Skill libraries fall back to PostgreSQL rather than the filesystem
        # store: every console surface (运行总览 / 实验工作台) reads the same
        # manifest+registry, so an OpenViking outage must not split them across
        # two different stores.
        fallback_backend = (
            "postgres"
            if bool(getattr(config, "storage_pg_enabled", False))
            and pg_dsn_available(pg_kwargs["pg_dsn"])
            else "local"
        )

        hub = cls(
            backend=backend,
            endpoint=endpoint,
            local_root=local_root,
            customer_id=customer_id,
            user_alias=getattr(config, "sharing_user_alias", ""),
            allow_fallback=allow_fallback,
            fallback_root=local_root,
            fallback_backend=fallback_backend if key_scope == "team" else "local",
            mirror_spool_dir=str(getattr(config, "sharing_skill_mirror_spool_dir", "") or ""),
            viking_namespace=namespace,
            tenant_id=tenant_id,
            **viking_kwargs,
            **pg_kwargs,
        )

        # When the primary bucket is local or PostgreSQL and the mirror is enabled,
        # attach a viking hub used by the mirror outbox to push the skills
        # subtree — PostgreSQL is also the OpenViking-outage fallback target, and
        # edits made during an outage must still reach OpenViking. The mirror
        # target bypasses the fallback (an unavailable OpenViking must surface as
        # a failed delivery, not silently write elsewhere).
        mirror_enabled = bool(getattr(config, "sharing_skill_mirror_enabled", True))
        if (
            isinstance(hub._bucket, (LocalObjectStore, PgObjectStore))
            and mirror_enabled
            and viking_endpoint
            and sharing_enabled
            and key_scope == "team"
        ):
            hub._mirror_viking_hub = cls(
                backend="viking",
                endpoint=viking_endpoint,
                customer_id=customer_id,
                user_alias=getattr(config, "sharing_user_alias", ""),
                allow_fallback=False,
                mirror_spool_dir=str(getattr(config, "sharing_skill_mirror_spool_dir", "") or ""),
                viking_namespace=namespace,
                tenant_id=tenant_id,
                **viking_kwargs,
            )
        return hub

    @classmethod
    def from_config(cls, config, tenant_id: str = "default") -> "SkillHub":
        """Build a hub for the caller's personal skills under ``resources``."""
        return cls._build(
            config,
            backend_field="sharing_skill_backend",
            customer_id=getattr(config, "sharing_viking_customer_id", ""),
            namespace="resources",
            key_scope="personal",
            tenant_id=tenant_id,
        )

    @classmethod
    def team_from_config(cls, config, tenant_id: str = "default") -> "SkillHub":
        """Build a hub for team-shared (``resources`` namespace) skills."""
        return cls._build(
            config,
            backend_field="sharing_skill_backend",
            customer_id="",
            namespace="resources",
            key_scope="team",
            tenant_id=tenant_id,
        )

    @classmethod
    def object_storage_from_config(
        cls, config, tenant_id: str = "default"
    ) -> Optional["SkillHub"]:
        """Build the object-store hub for skills and non-skill artifacts.

        Returns ``None`` when no object-storage backend is configured.
        """
        return cls._build(
            config,
            backend_field="sharing_session_backend",
            customer_id=getattr(config, "sharing_viking_customer_id", ""),
            namespace="resources",
            key_scope="team",
            allow_none=True,
            tenant_id=tenant_id,
        )

    # ------------------------------------------------------------------ #
    # Remote key helpers                                                   #
    # ------------------------------------------------------------------ #

    def _prefix(self) -> str:
        """Key prefix for skill artifacts.

        In by-peer mode skills are scoped under ``peers/<customer_id>/``.
        Without a customer id the prefix is empty.  Non-default tenants get
        an additional ``tenants/<tenant_id>/`` scope so their skills,
        manifest and registry are isolated from other tenants in the same
        object store — but ONLY for backends that share a physical key
        namespace across tenants (local filesystem, Viking). The PostgreSQL
        backend isolates rows with Row-Level Security (every connection is
        pinned to one tenant via ``app.tenant_id``), so a key prefix would
        both be redundant and break alignment with the engine, which writes
        bare ``skills/...`` keys into its per-tenant RLS scope.
        """
        parts: list[str] = []
        if self._tenant_id and self._tenant_id != "default" and not self._is_pg_backend:
            parts.append(f"tenants/{self._tenant_id}/")
        parts.append(peer_key_prefix(self._customer_id))
        return "".join(parts)

    def session_prefix(self) -> str:
        """Key prefix for the session queue consumed by skill evolution.

        Sessions are deliberately NOT partitioned per customer/peer: the queue
        feeds team-level skill evolution, which must see every peer's sessions
        together. The queue therefore pools at the team-shared root
        (``sessions/...`` under ``viking://resources/{root_prefix}/``).
        """
        return ""

    def _manifest_key(self) -> str:
        return f"{self._prefix()}manifest.json"

    def _skill_key(self, skill_name: str) -> str:
        return f"{self._prefix()}skills/{skill_name}/SKILL.md"

    def _skill_files_prefix(self, skill_name: str) -> str:
        return f"{self._prefix()}skills/{skill_name}/files/"

    def _skill_bundle_key(self, skill_name: str, rel_path: str) -> str:
        clean = str(rel_path or "").strip().replace("\\", "/")
        if clean == "SKILL.md":
            return self._skill_key(skill_name)
        return f"{self._skill_files_prefix(skill_name)}{clean}"

    def _iter_remote_keys(self, prefix: str):
        return self._bucket.iter_objects(prefix=prefix)

    def _delete_remote_bundle_extras(self, skill_name: str, keep_paths: Collection[str]) -> None:
        keep_keys = {self._skill_bundle_key(skill_name, rel_path) for rel_path in keep_paths if rel_path != "SKILL.md"}
        for obj in self._iter_remote_keys(self._skill_files_prefix(skill_name)):
            key = str(getattr(obj, "key", "") or "")
            if key and key not in keep_keys:
                self._bucket.delete_object(key)

    def _download_skill_bundle(self, skill_name: str, record: dict[str, Any]) -> dict[str, bytes]:
        bundle: dict[str, bytes] = {}
        file_entries = record.get("files")
        if isinstance(file_entries, list) and file_entries:
            for item in file_entries:
                rel_path = str((item or {}).get("path") or "").strip().replace("\\", "/")
                if not rel_path:
                    continue
                key = self._skill_bundle_key(skill_name, rel_path)
                bundle[rel_path] = self._bucket.get_object(key).read()
        else:
            bundle["SKILL.md"] = self._bucket.get_object(self._skill_key(skill_name)).read()
        return bundle

    def _skill_version_prefix(self, skill_name: str, version: int) -> str:
        return f"{self._prefix()}skills/{skill_name}/versions/v{max(1, int(version or 1))}/"

    def _skill_version_bundle_key(self, skill_name: str, version: int, rel_path: str) -> str:
        clean = str(rel_path or "").strip().replace("\\", "/")
        if clean == "SKILL.md":
            return f"{self._skill_version_prefix(skill_name, version)}SKILL.md"
        return f"{self._skill_version_prefix(skill_name, version)}files/{clean}"

    def _skill_version_record_key(self, skill_name: str, version: int) -> str:
        return f"{self._skill_version_prefix(skill_name, version)}bundle.json"

    def _save_version_bundle(self, skill_name: str, version: int, bundle_files: dict[str, bytes]) -> dict[str, Any]:
        keep_keys: set[str] = set()
        stored_bundle: dict[str, bytes] = {}
        for rel_path, data in sorted(bundle_files.items()):
            key = self._skill_version_bundle_key(skill_name, version, rel_path)
            keep_keys.add(key)
            self._bucket.put_object(key, data)
            stored_bundle[rel_path] = self._bucket.get_object(key).read()
        for obj in self._iter_remote_keys(f"{self._skill_version_prefix(skill_name, version)}files/"):
            key = str(getattr(obj, "key", "") or "")
            if key and key not in keep_keys:
                self._bucket.delete_object(key)
        record = {
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "tree_sha256": bundle_tree_sha256(stored_bundle),
            "files": bundle_file_records(stored_bundle),
        }
        self._bucket.put_object(
            self._skill_version_record_key(skill_name, version),
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return record

    @staticmethod
    def _local_bundle_matches_record(skill_dir: str, record: dict[str, Any]) -> bool:
        bundle, _records, tree_sha = read_skill_bundle_with_meta(skill_dir)
        if not bundle:
            return False
        if record.get("format") == "bundle_v1":
            return str(record.get("tree_sha256") or "") == tree_sha

        try:
            skill_md = bundle_entrypoint_bytes(bundle)
        except Exception:
            return False
        skill_sha = hashlib.sha256(skill_md).hexdigest()
        return bundle_has_only_entrypoint(bundle) and str(record.get("sha256") or "") == skill_sha

    # ------------------------------------------------------------------ #
    # Manifest operations                                                  #
    # ------------------------------------------------------------------ #

    def _load_remote_manifest(self) -> dict[str, dict[str, Any]]:
        """Load manifest.json from storage. Returns ``{skill_name: record}``."""
        key = self._manifest_key()
        try:
            result = self._bucket.get_object(key)
            content = result.read().decode("utf-8")
        except Exception as e:
            if is_not_found_error(e):
                return {}
            logger.warning("[SkillHub] failed to load manifest: %s", e)
            return {}

        manifest: dict[str, dict[str, Any]] = {}
        for line in content.strip().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                name = rec.get("name", "")
                if name:
                    manifest[name] = rec
            except json.JSONDecodeError:
                continue
        return manifest

    def _save_remote_manifest(self, manifest: dict[str, dict[str, Any]]) -> None:
        """Write the full manifest back to storage."""
        lines = [json.dumps(rec, ensure_ascii=False) for rec in manifest.values()]
        content = "\n".join(lines) + "\n" if lines else ""
        self._bucket.put_object(self._manifest_key(), content.encode("utf-8"))

    def publish_bundle(
        self,
        skill_name: str,
        bundle_files: dict[str, bytes],
        *,
        action: str = "push",
        max_conflict_retries: int = 3,
    ) -> dict[str, Any]:
        """Publish one complete bundle, using a CAS batch on capable stores."""
        name = self._clean_skill_name(skill_name)
        bundle = coerce_skill_bundle(bundle_files)
        entrypoint = bundle_entrypoint_bytes(bundle)
        tree_sha = bundle_tree_sha256(bundle)
        file_records = bundle_file_records(bundle)

        with _BUNDLE_WRITE_LOCK:
            if bool(getattr(self._bucket, "native_batch_write", False)):
                attempts = max(1, int(max_conflict_retries))
                for attempt in range(attempts):
                    manifest, manifest_precondition = load_manifest_snapshot(
                        self._bucket,
                        self._prefix(),
                    )
                    current = manifest.get(name) or {}
                    if (
                        current.get("format") == "bundle_v1"
                        and str(current.get("tree_sha256") or "") == tree_sha
                    ):
                        return {
                            "uploaded": 0,
                            "skipped": 1,
                            "record": dict(current),
                        }

                    registry = SkillIDRegistry()
                    _, registry_precondition = registry.merge_from_oss_snapshot(
                        self._bucket,
                        self._prefix(),
                    )
                    version = registry.record_update(
                        name,
                        hashlib.sha256(entrypoint).hexdigest(),
                        action=action,
                        bundle_record={
                            "format": "bundle_v1",
                            "entrypoint": "SKILL.md",
                            "tree_sha256": tree_sha,
                            "files": file_records,
                        },
                    )
                    entry: dict[str, Any] = {
                        **current,
                        "name": name,
                        "skill_id": registry.get_or_create(name),
                        "version": version,
                        "sha256": hashlib.sha256(entrypoint).hexdigest(),
                        "tree_sha256": tree_sha,
                        "format": "bundle_v1",
                        "entrypoint": "SKILL.md",
                        "files": file_records,
                        "uploaded_by": self._user_alias,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    }
                    frontmatter.enrich_manifest_entry_from_raw(
                        entry,
                        entrypoint,
                        name,
                    )
                    entry.pop("deleted", None)
                    manifest[name] = entry
                    try:
                        publish_skill_bundle_batch(
                            self._bucket,
                            self._prefix(),
                            name,
                            version,
                            bundle,
                            manifest=manifest,
                            registry_bytes=registry.to_bytes(),
                            fixed_preconditions={
                                self._manifest_key(): manifest_precondition,
                                f"{self._prefix()}evolve_skill_registry.json": (
                                    registry_precondition
                                ),
                            },
                        )
                    except RuntimeError:
                        if attempt + 1 >= attempts:
                            raise
                        continue
                    registry.mark_persisted()
                    self.mirror_enqueue_push(name, bundle)
                    return {"uploaded": 1, "skipped": 0, "record": entry}

            manifest = self._load_remote_manifest()
            current = manifest.get(name) or {}
            if (
                current.get("format") == "bundle_v1"
                and str(current.get("tree_sha256") or "") == tree_sha
            ):
                return {
                    "uploaded": 0,
                    "skipped": 1,
                    "record": dict(current),
                }
            registry = SkillIDRegistry()
            registry.load_from_oss(self._bucket, self._prefix())
            entry = self._commit_bundle(
                name,
                bundle,
                action=action,
                manifest=manifest,
                registry=registry,
            )
            self._save_remote_manifest(manifest)
            registry.save_to_oss(self._bucket, self._prefix())
            self.mirror_enqueue_push(name, bundle)
            return {"uploaded": 1, "skipped": 0, "record": entry}

    def _commit_bundle(
        self,
        skill_name: str,
        bundle_files: dict[str, bytes],
        *,
        action: str,
        manifest: dict[str, dict[str, Any]],
        registry: SkillIDRegistry,
        skill_path: str = "",
    ) -> dict[str, Any]:
        """Publish *bundle_files* as the live skill and record a new version.

        Shared by the directory push (:meth:`push_skills`) and the hub-only file
        API (:meth:`write_skill_file`), so both paths produce identical
        live-object, version-snapshot, manifest and registry state. Updates
        ``manifest`` in place and returns the new manifest entry; the caller owns
        persisting manifest + registry.
        """
        entrypoint = bundle_entrypoint_bytes(bundle_files)
        self._bucket.put_object(self._skill_key(skill_name), entrypoint)
        # Re-read so the recorded hash/tree describe what storage actually kept.
        stored_entrypoint = self._bucket.get_object(self._skill_key(skill_name)).read()
        stored_bundle = {**bundle_files, "SKILL.md": stored_entrypoint}
        file_records = bundle_file_records(stored_bundle)
        tree_sha = bundle_tree_sha256(stored_bundle)
        sha = hashlib.sha256(stored_entrypoint).hexdigest()
        for rel_path, data in sorted(stored_bundle.items()):
            if rel_path == "SKILL.md":
                continue
            self._bucket.put_object(self._skill_bundle_key(skill_name, rel_path), data)
        self._delete_remote_bundle_extras(skill_name, stored_bundle.keys())

        version = registry.record_update(
            skill_name,
            sha,
            action=action,
            bundle_record={
                "format": "bundle_v1",
                "entrypoint": "SKILL.md",
                "tree_sha256": tree_sha,
                "files": file_records,
            },
        )
        self._save_version_bundle(skill_name, version, stored_bundle)

        entry: dict[str, Any] = {
            **(manifest.get(skill_name) or {}),
            "name": skill_name,
            "skill_id": registry.get_or_create(skill_name),
            "version": version,
            "sha256": sha,
            "tree_sha256": tree_sha,
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "files": file_records,
            "uploaded_by": self._user_alias,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if skill_path:
            frontmatter.enrich_manifest_entry(entry, skill_path)
        else:
            frontmatter.enrich_manifest_entry_from_raw(entry, stored_entrypoint, skill_name)
        entry.pop("deleted", None)
        manifest[skill_name] = entry
        return entry

    # ------------------------------------------------------------------ #
    # Push (working directory -> durable store)                            #
    # ------------------------------------------------------------------ #

    def push_skills(
        self,
        skills_dir: str,
        skill_filter: Optional[dict[str, Any]] = None,
        include_names: Optional[Collection[str]] = None,
    ) -> dict[str, int]:
        """Upload new/changed skills from local directory to shared storage.

        Parameters
        ----------
        skills_dir:
            Path to the local skills directory.
        skill_filter:
            Optional quality gate. When provided, must contain ``"stats"``
            (skill_name → stats record), ``"min_injections"`` (skills below this
            are still on probation and not uploaded) and ``"min_effectiveness"``
            (skills below this after probation are blocked). Skills that have
            *never* been injected are treated as brand-new and allowed through.
        include_names:
            Optional subset of skill names to push; every other local skill is
            ignored. Used by the management UI to sync a single just-edited
            skill without scanning the whole library.

        Returns ``{"uploaded": N, "skipped": M, "filtered": F, "total_local": T}``.
        """
        paths = layout.skill_md_paths(skills_dir)
        include_set = {str(n or "").strip() for n in (include_names or []) if str(n or "").strip()}
        if include_set:
            paths = [p for p in paths if os.path.basename(os.path.dirname(p)) in include_set]
        if not paths:
            logger.info("[SkillHub] no local skills to push")
            return {"uploaded": 0, "skipped": 0, "filtered": 0, "total_local": 0}

        manifest = self._load_remote_manifest()
        uploaded = 0
        skipped = 0
        filtered = 0

        stats = (skill_filter or {}).get("stats", {})
        min_inj = (skill_filter or {}).get("min_injections", 0)
        min_eff = (skill_filter or {}).get("min_effectiveness", 0.0)
        use_filter = skill_filter is not None

        for path in paths:
            skill_name = os.path.basename(os.path.dirname(path))
            skill_dir = os.path.dirname(path)

            if use_filter and skill_name in stats:
                entry = stats[skill_name]
                inj = entry.get("inject_count", 0)
                eff = entry.get("effectiveness", 0.5)
                if inj >= min_inj and eff < min_eff:
                    logger.info(
                        "[SkillHub] filtered out skill %s (effectiveness=%.2f < %.2f, injections=%d)",
                        skill_name,
                        eff,
                        min_eff,
                        inj,
                    )
                    filtered += 1
                    continue

            bundle_files = read_skill_bundle_with_meta(skill_dir)[0]

            remote_rec = manifest.get(skill_name)
            if remote_rec and self._local_bundle_matches_record(skill_dir, remote_rec):
                skipped += 1
                continue

            result = self.publish_bundle(
                skill_name,
                bundle_files,
                action="push",
            )
            if int(result.get("uploaded") or 0):
                uploaded += 1
                logger.info("[SkillHub] pushed skill: %s", skill_name)
                manifest[skill_name] = dict(result.get("record") or {})
            else:
                skipped += 1

        logger.info(
            "[SkillHub] push complete: %d uploaded, %d skipped, %d filtered, %d total",
            uploaded,
            skipped,
            filtered,
            len(paths),
        )
        return {"uploaded": uploaded, "skipped": skipped, "filtered": filtered, "total_local": len(paths)}

    # ------------------------------------------------------------------ #
    # Local skill discovery                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _list_local_skill_dirs(skills_dir: str) -> dict[str, list[str]]:
        """Return ``{skill_name: [skill_dir, ...]}`` for local skill folders."""
        out: dict[str, list[str]] = {}
        if not os.path.isdir(skills_dir):
            return out
        if layout.is_hermes_skill_root(skills_dir):
            for path in sorted(glob.glob(os.path.join(skills_dir, "**", "SKILL.md"), recursive=True)):
                skill_dir = os.path.dirname(path)
                out.setdefault(os.path.basename(skill_dir), []).append(skill_dir)
            return out
        for entry in os.scandir(skills_dir):
            if not entry.is_dir():
                continue
            if os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                out.setdefault(entry.name, []).append(entry.path)
        return out

    @staticmethod
    def _resolve_pull_target_dir(
        skills_dir: str,
        skill_name: str,
        category: str,
        local_dirs_by_name: dict[str, list[str]],
    ) -> str:
        """Choose the local directory to write a pulled skill into.

        Honors an existing single category-nested location under the hermes
        root when the incoming category is unspecified; otherwise the
        category-derived path.
        """
        target = layout.skill_dir_for(skills_dir, skill_name, category)
        if not layout.is_hermes_skill_root(skills_dir):
            return target

        existing_dirs = local_dirs_by_name.get(skill_name) or []
        if not existing_dirs:
            return target

        if str(category or "general").strip() == "general":
            nested = [path for path in existing_dirs if len(os.path.relpath(path, skills_dir).split(os.sep)) >= 2]
            if len(nested) == 1:
                return nested[0]

        return target

    @staticmethod
    def _remove_duplicate_local_skill_dirs(
        skill_name: str,
        keep_dir: str,
        local_dirs_by_name: dict[str, list[str]],
    ) -> None:
        keep_real = os.path.realpath(keep_dir)
        for skill_dir in local_dirs_by_name.get(skill_name) or []:
            if os.path.realpath(skill_dir) == keep_real:
                continue
            if not os.path.isdir(skill_dir):
                continue
            shutil.rmtree(skill_dir)
            logger.info("[SkillHub] removed duplicate local skill dir: %s", skill_dir)

    @staticmethod
    def _prune_backups(backup_root: str, prefix: str, keep: int = 3) -> None:
        """Keep only the newest ``keep`` backups for the current skills dir."""
        try:
            names = sorted(n for n in os.listdir(backup_root) if n.startswith(prefix))
        except Exception:
            return
        to_delete = names[:-keep] if keep > 0 else names
        for name in to_delete:
            try:
                shutil.rmtree(os.path.join(backup_root, name))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Pull (durable store -> working directory)                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pull_result(
        include_meta: dict[str, Any],
        *,
        downloaded: int,
        skipped: int,
        deleted: int,
        total_remote: int,
        restored_from_backup: bool,
        backup_dir: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "downloaded": downloaded,
            "skipped": skipped,
            "deleted": deleted,
            "total_remote": total_remote,
            "restored_from_backup": restored_from_backup,
            "backup_dir": backup_dir,
        }
        if include_meta:
            payload.update(include_meta)
        return payload

    def pull_skills(
        self,
        skills_dir: str,
        mirror: bool = True,
        skip_names: Optional[Collection[str]] = None,
        include_names: Optional[Collection[str]] = None,
    ) -> dict[str, Any]:
        """Pull stored skills into a local working directory.

        When *mirror* is ``True`` (default) local skill folders absent from the
        remote manifest are deleted, with backup + rollback safety. When
        ``False`` (or when *include_names* is given) an incremental pull only
        downloads/updates remote skills and never deletes local extras.

        Parameters
        ----------
        skip_names:
            Skill names preserved from local disk during this pull.
        include_names:
            Optional subset of remote skill names to download; forces
            incremental mode to avoid deleting unrelated local skills.

        Returns a dict with ``downloaded``/``skipped``/``deleted``/
        ``total_remote``/``restored_from_backup``/``backup_dir`` (plus
        ``requested``/``matched_remote``/``missing``/``missing_names`` when
        *include_names* is used).
        """
        os.makedirs(skills_dir, exist_ok=True)
        local_dirs_by_name = self._list_local_skill_dirs(skills_dir)
        manifest = self._load_remote_manifest()

        skip_set = {str(name or "").strip() for name in (skip_names or []) if str(name or "").strip()}
        include_set = {str(name or "").strip() for name in (include_names or []) if str(name or "").strip()}
        if include_set and mirror:
            mirror = False
        if include_set:
            manifest = {name: rec for name, rec in manifest.items() if name in include_set}

        missing_names = sorted(include_set - set(manifest))
        include_meta: dict[str, Any] = {}
        if include_set:
            include_meta = {
                "requested": len(include_set),
                "matched_remote": len(manifest),
                "missing": len(missing_names),
                "missing_names": missing_names,
            }

        if not manifest:
            # Empty/failed manifest is a no-op to avoid an accidental wipe.
            if include_set:
                logger.info(
                    "[SkillHub] none of the requested remote skills matched the manifest: %s",
                    ", ".join(missing_names) or "(empty request)",
                )
            else:
                logger.warning("[SkillHub] remote manifest empty; skip mirror pull (downloaded=0 skipped=0 deleted=0)")
            return self._pull_result(
                include_meta,
                downloaded=0,
                skipped=0,
                deleted=0,
                total_remote=0,
                restored_from_backup=False,
                backup_dir="",
            )

        if mirror:
            return self._pull_mirror(skills_dir, manifest, local_dirs_by_name, skip_set, include_meta)
        return self._pull_incremental(skills_dir, manifest, local_dirs_by_name, skip_set, include_meta)

    def _pull_incremental(
        self,
        skills_dir: str,
        manifest: dict[str, dict[str, Any]],
        local_dirs_by_name: dict[str, list[str]],
        skip_set: set[str],
        include_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Download/update remote skills without deleting local extras."""
        downloaded = 0
        skipped = 0
        for name, rec in manifest.items():
            category = str(rec.get("category", "general") or "general")
            local_dir = self._resolve_pull_target_dir(skills_dir, name, category, local_dirs_by_name)
            local_path = os.path.join(local_dir, "SKILL.md")

            if name in skip_set and os.path.exists(local_path):
                skipped += 1
                self._remove_duplicate_local_skill_dirs(name, local_dir, local_dirs_by_name)
                logger.info("[SkillHub] preserved local skill during pull: %s", name)
                continue

            if os.path.isdir(local_dir) and self._local_bundle_matches_record(local_dir, rec):
                skipped += 1
                self._remove_duplicate_local_skill_dirs(name, local_dir, local_dirs_by_name)
                continue

            try:
                bundle = self._download_skill_bundle(name, rec)
            except Exception as e:
                logger.warning("[SkillHub] failed to download skill %s: %s", name, e)
                continue

            write_skill_bundle(local_dir, bundle, clean=True)
            downloaded += 1
            self._remove_duplicate_local_skill_dirs(name, local_dir, local_dirs_by_name)
            logger.info("[SkillHub] pulled skill: %s", name)

        logger.info(
            "[SkillHub] incremental pull complete: %d downloaded, %d skipped, %d total remote",
            downloaded,
            skipped,
            len(manifest),
        )
        return self._pull_result(
            include_meta,
            downloaded=downloaded,
            skipped=skipped,
            deleted=0,
            total_remote=len(manifest),
            restored_from_backup=False,
            backup_dir="",
        )

    def _pull_mirror(
        self,
        skills_dir: str,
        manifest: dict[str, dict[str, Any]],
        local_dirs_by_name: dict[str, list[str]],
        skip_set: set[str],
        include_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Full mirror pull: stage into a temp dir, then swap in with rollback."""
        local_skills = {name: dirs[-1] for name, dirs in local_dirs_by_name.items() if dirs}
        parent_dir = os.path.dirname(os.path.abspath(skills_dir))
        base_name = os.path.basename(os.path.abspath(skills_dir))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_root = os.path.join(parent_dir, _BACKUP_DIRNAME)
        os.makedirs(backup_root, exist_ok=True)
        backup_prefix = f"{base_name}_"
        backup_dir = os.path.join(backup_root, f"{backup_prefix}{stamp}")
        staging_dir = os.path.join(parent_dir, f"{_STAGE_PREFIX}{base_name}_{stamp}")

        try:
            shutil.copytree(skills_dir, backup_dir)
        except Exception as e:
            logger.warning("[SkillHub] backup before pull failed: %s", e)
            return self._pull_result(
                include_meta,
                downloaded=0,
                skipped=0,
                deleted=0,
                total_remote=len(manifest),
                restored_from_backup=False,
                backup_dir="",
            )

        os.makedirs(staging_dir, exist_ok=True)
        resolved_targets: dict[str, str] = {}
        downloaded = 0
        skipped = 0
        deleted = 0

        try:
            for name, rec in manifest.items():
                category = str(rec.get("category", "general") or "general")
                target_dir = self._resolve_pull_target_dir(skills_dir, name, category, local_dirs_by_name)
                resolved_targets[name] = target_dir
                local_path = os.path.join(target_dir, "SKILL.md")
                staged_dir = os.path.join(staging_dir, os.path.relpath(target_dir, skills_dir))

                if name in skip_set and os.path.exists(local_path):
                    skipped += 1
                    if os.path.isdir(target_dir):
                        shutil.copytree(target_dir, staged_dir, dirs_exist_ok=True)
                    logger.info("[SkillHub] preserved local skill during pull: %s", name)
                    continue

                if os.path.isdir(target_dir) and self._local_bundle_matches_record(target_dir, rec):
                    skipped += 1
                    shutil.copytree(target_dir, staged_dir, dirs_exist_ok=True)
                    continue

                try:
                    bundle = self._download_skill_bundle(name, rec)
                except Exception as e:
                    raise RuntimeError(f"failed to download skill {name}: {e}") from e

                write_skill_bundle(staged_dir, bundle, clean=True)
                downloaded += 1
                logger.info("[SkillHub] pulled skill: %s", name)

            remote_names = set(manifest.keys())
            local_names = set(local_skills.keys())
            for stale in sorted(local_names - remote_names):
                shutil.rmtree(local_skills[stale], ignore_errors=False)
                deleted += 1

            for name in sorted(remote_names):
                rec = manifest.get(name, {})
                category = str(rec.get("category", "general") or "general")
                dst_dir = resolved_targets.get(name) or self._resolve_pull_target_dir(
                    skills_dir, name, category, local_dirs_by_name
                )
                src_dir = os.path.join(staging_dir, os.path.relpath(dst_dir, skills_dir))
                if os.path.isdir(dst_dir):
                    shutil.rmtree(dst_dir)
                os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
                shutil.move(src_dir, dst_dir)
                self._remove_duplicate_local_skill_dirs(name, dst_dir, local_dirs_by_name)

        except Exception as e:
            logger.warning("[SkillHub] mirror pull failed, restoring backup: %s", e)
            restored_from_backup = False
            try:
                if os.path.isdir(skills_dir):
                    shutil.rmtree(skills_dir)
                shutil.copytree(backup_dir, skills_dir)
                restored_from_backup = True
                logger.info("[SkillHub] local skills restored from backup: %s", backup_dir)
            except Exception as restore_err:
                logger.error("[SkillHub] backup restore failed: %s", restore_err)
            return self._pull_result(
                include_meta,
                downloaded=0,
                skipped=0,
                deleted=0,
                total_remote=len(manifest),
                restored_from_backup=restored_from_backup,
                backup_dir=backup_dir,
            )
        finally:
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

        logger.info(
            "[SkillHub] pull complete: %d downloaded, %d skipped, %d deleted, %d total remote",
            downloaded,
            skipped,
            deleted,
            len(manifest),
        )
        self._prune_backups(backup_root, backup_prefix, keep=3)
        return self._pull_result(
            include_meta,
            downloaded=downloaded,
            skipped=skipped,
            deleted=deleted,
            total_remote=len(manifest),
            restored_from_backup=False,
            backup_dir=backup_dir,
        )

    # ------------------------------------------------------------------ #
    # Delete (cloud)                                                       #
    # ------------------------------------------------------------------ #

    def delete_skill(self, skill_name: str) -> dict[str, Any]:
        """Remove a skill from shared storage: manifest, bundle, and versions.

        Idempotent — deleting an absent skill returns ``{"deleted": False}``
        rather than raising, so the management UI can keep local and remote in
        sync even after a partial failure.
        """
        name = str(skill_name or "").strip()
        if not name:
            return {"deleted": False, "name": name}

        manifest = self._load_remote_manifest()
        existed = name in manifest

        # Remove every object under the skill's key subtree (SKILL.md, files/,
        # and versions/). Iterating the prefix covers bundle + version blobs in
        # one pass regardless of how many versions accumulated.
        subtree = f"{self._prefix()}skills/{name}/"
        for obj in self._iter_remote_keys(subtree):
            key = str(getattr(obj, "key", "") or "")
            if key:
                self._bucket.delete_object(key)

        if existed:
            manifest.pop(name, None)
            self._save_remote_manifest(manifest)

        registry = SkillIDRegistry()
        registry.load_from_oss(self._bucket, self._prefix())
        if name in registry.all_ids():
            registry.record_update(name, "", action="delete")
            registry.save_to_oss(self._bucket, self._prefix())

        logger.info("[SkillHub] deleted remote skill: %s (existed=%s)", name, existed)
        self.mirror_enqueue_delete(name)
        return {"deleted": existed, "name": name}

    # ------------------------------------------------------------------ #
    # List / sync                                                          #
    # ------------------------------------------------------------------ #

    def list_remote(self) -> list[dict[str, Any]]:
        """Return a list of skill metadata dicts from the remote manifest."""
        return list(self._load_remote_manifest().values())

    def describe_source(self) -> dict[str, Any]:
        """Describe the live backend, for consoles that show where skills live.

        ``backend`` is ``viking`` / ``postgres`` / ``local`` (``memory`` for the
        in-process test double); ``fallback`` is true when OpenViking was
        unreachable and the configured fallback store is in use instead.
        """
        bucket = self._bucket
        if isinstance(bucket, PgObjectStore):
            backend = "postgres"
        elif isinstance(bucket, LocalObjectStore):
            backend = "local"
        elif isinstance(bucket, InMemoryObjectStore):
            backend = "memory"
        else:
            backend = "viking"
        return {
            "backend": backend,
            "fallback": bool(getattr(bucket, "fallback_active", False)),
            "reason": str(getattr(bucket, "fallback_reason", "") or ""),
        }

    # ------------------------------------------------------------------ #
    # Hub-only bundle file access (no local working copy)                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean_skill_name(skill_name: str) -> str:
        name = str(skill_name or "").strip()
        if not name:
            raise ValueError("skill name is required")
        return name

    @staticmethod
    def _clean_rel_path(rel_path: str) -> str:
        """Validate a bundle-relative path (no traversal, no ignored files)."""
        clean = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
        if (
            not clean
            or ":" in clean
            or any(part in {"", ".", ".."} for part in clean.split("/"))
            or is_ignored_bundle_rel_path(clean)
        ):
            raise ValueError(f"invalid skill file path: {rel_path!r}")
        return clean

    def skill_file_records(self, skill_name: str) -> list[dict[str, Any]]:
        """Return the current bundle's file records without downloading them."""
        record = self._load_remote_manifest().get(self._clean_skill_name(skill_name)) or {}
        files = record.get("files")
        if isinstance(files, list) and files:
            return [dict(item) for item in files if isinstance(item, dict)]
        if record:
            return [{
                "path": "SKILL.md",
                "sha256": str(record.get("sha256") or ""),
                "size": int(record.get("size") or 0),
            }]
        raise FileNotFoundError(f"skill not found in storage: {skill_name}")

    def read_skill_bundle(self, skill_name: str) -> dict[str, bytes]:
        """Read a skill's live bundle as ``{relative_path: bytes}``."""
        name = self._clean_skill_name(skill_name)
        record = self._load_remote_manifest().get(name)
        if not record:
            raise FileNotFoundError(f"skill not found in storage: {name}")
        return self._download_skill_bundle(name, record)

    def read_skill_file(self, skill_name: str, rel_path: str) -> bytes:
        """Read one file from a skill's live bundle."""
        name = self._clean_skill_name(skill_name)
        clean = self._clean_rel_path(rel_path)
        if clean == "SKILL.md":
            try:
                return self._bucket.get_object(self._skill_key(name)).read()
            except Exception as exc:  # noqa: BLE001 - normalise to not-found
                if is_not_found_error(exc):
                    raise FileNotFoundError(f"skill not found in storage: {name}") from exc
                raise
        return self._bucket.get_object(self._skill_bundle_key(name, clean)).read()

    def write_skill_file(
        self,
        skill_name: str,
        rel_path: str,
        data: bytes,
        *,
        expected_sha256: Optional[str] = None,
        action: str = "edit",
    ) -> dict[str, Any]:
        """Write one file into a skill's live bundle and record a new version.

        Hub-only counterpart of the console workbench: the skill never touches a
        local working copy, so OpenViking (or its PostgreSQL fallback) stays the
        single source every page reads. ``expected_sha256`` enforces optimistic
        concurrency — ``None`` means "file must not exist yet", a string must
        match the stored file's hash; a mismatch raises
        :class:`SkillFileConflictError`.

        Returns the new manifest record ``{name, version, sha256, tree_sha256,
        files, ...}``.
        """
        name = self._clean_skill_name(skill_name)
        clean = self._clean_rel_path(rel_path)
        payload = bytes(data)
        with _BUNDLE_WRITE_LOCK:
            bundle = self.read_skill_bundle(name)
            stored = bundle.get(clean)
            stored_sha = hashlib.sha256(stored).hexdigest() if stored is not None else None
            if expected_sha256 != stored_sha:
                raise SkillFileConflictError(
                    f"{name}/{clean} changed in storage; reload before saving"
                )
            bundle[clean] = payload
            result = self.publish_bundle(name, bundle, action=action)
        return dict(result["record"])

    def delete_skill_file(self, skill_name: str, rel_path: str) -> dict[str, Any]:
        """Remove one (non-entrypoint) file from a skill and record a version."""
        name = self._clean_skill_name(skill_name)
        clean = self._clean_rel_path(rel_path)
        if clean == "SKILL.md":
            raise ValueError("SKILL.md 是技能入口文件，不能删除")
        with _BUNDLE_WRITE_LOCK:
            bundle = self.read_skill_bundle(name)
            if clean not in bundle:
                raise FileNotFoundError(f"{name}/{clean} not found")
            bundle.pop(clean)
            result = self.publish_bundle(name, bundle, action="edit")
        return dict(result["record"])

    # ------------------------------------------------------------------ #
    # Version history + rollback                                           #
    # ------------------------------------------------------------------ #

    def _load_registry(self) -> SkillIDRegistry:
        registry = SkillIDRegistry()
        registry.load_from_oss(self._bucket, self._prefix())
        return registry

    def list_versions(self, skill_name: str) -> dict[str, Any]:
        """Return the version history for one skill from the ID registry.

        Shape: ``{skill_id, current_version, versions: [int, ...],
        history: [{version, action, timestamp, ...}, ...]}``. ``versions`` is a
        descending list of every version we can actually reconstruct from
        storage (each has a ``versions/v{n}/`` bundle), so the UI never offers a
        version that cannot be viewed or rolled back to.
        """
        name = str(skill_name or "").strip()
        registry = self._load_registry()
        entry = registry._map.get(name, {}) if name else {}
        current = int(entry.get("version") or 0)
        history = entry.get("history") if isinstance(entry.get("history"), list) else []

        available: list[int] = []
        for version in range(current, 0, -1):
            if self._version_bundle_exists(name, version):
                available.append(version)
        # The current live version may predate per-version bundles; always keep
        # it selectable so the modal can show at least the live content.
        if current > 0 and current not in available:
            available.insert(0, current)

        return {
            "name": name,
            "skill_id": str(entry.get("skill_id") or registry.get_or_create(name) if name else ""),
            "current_version": current,
            "versions": available,
            "history": history,
        }

    def _version_bundle_exists(self, skill_name: str, version: int) -> bool:
        try:
            self._bucket.get_object(self._skill_version_record_key(skill_name, version))
            return True
        except Exception:
            return False

    def _read_version_bundle(self, skill_name: str, version: int) -> dict[str, bytes]:
        """Reconstruct a specific version's bundle from ``versions/v{n}/``.

        Falls back to the live (non-versioned) bundle only when the requested
        version equals the current manifest version and no versioned snapshot
        exists (older skills pushed before per-version snapshots landed).
        """
        record_bytes: Optional[bytes] = None
        try:
            record_bytes = self._bucket.get_object(
                self._skill_version_record_key(skill_name, version)
            ).read()
        except Exception:
            record_bytes = None

        if record_bytes is not None:
            record = json.loads(record_bytes.decode("utf-8"))
            bundle: dict[str, bytes] = {}
            file_entries = record.get("files")
            if isinstance(file_entries, list) and file_entries:
                for item in file_entries:
                    rel_path = str((item or {}).get("path") or "").strip().replace("\\", "/")
                    if not rel_path:
                        continue
                    key = self._skill_version_bundle_key(skill_name, version, rel_path)
                    bundle[rel_path] = self._bucket.get_object(key).read()
            else:
                key = self._skill_version_bundle_key(skill_name, version, "SKILL.md")
                bundle["SKILL.md"] = self._bucket.get_object(key).read()
            return bundle

        manifest = self._load_remote_manifest()
        rec = manifest.get(skill_name)
        if rec and int(rec.get("version") or 0) == int(version):
            return self._download_skill_bundle(skill_name, rec)
        raise FileNotFoundError(f"version v{version} of {skill_name} not found in storage")

    def read_version_bundle(self, skill_name: str, version: int) -> dict[str, bytes]:
        """Public accessor for a full version bundle (skillopt rollout delivery)."""
        return self._read_version_bundle(skill_name, int(version))

    def get_version_detail(self, skill_name: str, version: int) -> dict[str, Any]:
        """Return one version's SKILL.md content + parsed metadata for the UI."""
        name = str(skill_name or "").strip()
        info = self.list_versions(name)
        bundle = self._read_version_bundle(name, int(version))
        raw_md = bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
        parsed = frontmatter._load_frontmatter_from_raw(raw_md) or {}
        body = raw_md
        split = frontmatter._split_frontmatter(raw_md)
        if split is not None:
            body = split[1]
        return {
            "name": name,
            "skill_id": info.get("skill_id") or name,
            "version": int(version),
            "current_version": info.get("current_version") or 0,
            "is_current": int(version) == int(info.get("current_version") or 0),
            "versions": info.get("versions") or [],
            "description": str(parsed.get("description") or ""),
            "category": frontmatter.resolve_category(parsed) or "general",
            "content": body,
            "raw_md": raw_md,
            "tree_sha256": bundle_tree_sha256(bundle),
            "files": bundle_file_records(bundle),
        }

    def rollback_skill(self, skill_name: str, target_version: int) -> dict[str, Any]:
        """Republish an older version's content as a new current version.

        Rollback never rewrites history: it reads ``target_version``'s bundle,
        writes it back as the live bundle, and records a *new* version (so the
        chain stays append-only and auditable). Returns
        ``{name, restored_from, new_version}``.
        """
        name = str(skill_name or "").strip()
        if not name:
            raise ValueError("skill name is required")
        target = int(target_version)
        bundle = self._read_version_bundle(name, target)
        if "SKILL.md" not in bundle:
            raise FileNotFoundError(f"version v{target} of {name} has no SKILL.md")

        skill_md = bundle_entrypoint_bytes(bundle)
        local_sha = hashlib.sha256(skill_md).hexdigest()
        tree_sha = bundle_tree_sha256(bundle)
        bundle_records = bundle_file_records(bundle)

        # Write the restored bundle back as the live skill objects.
        self._bucket.put_object(self._skill_key(name), skill_md)
        for rel_path, data in sorted(bundle.items()):
            if rel_path == "SKILL.md":
                continue
            self._bucket.put_object(self._skill_bundle_key(name, rel_path), data)
        self._delete_remote_bundle_extras(name, bundle.keys())

        bundle_record = {
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "tree_sha256": tree_sha,
            "files": bundle_records,
        }
        registry = self._load_registry()
        new_version = registry.record_update(
            name, local_sha, action=f"rollback:v{target}", bundle_record=bundle_record
        )
        self._save_version_bundle(name, new_version, bundle)

        manifest = self._load_remote_manifest()
        existing = manifest.get(name, {})
        manifest[name] = {
            **existing,
            "name": name,
            "skill_id": registry.get_or_create(name),
            "version": new_version,
            "sha256": local_sha,
            "tree_sha256": tree_sha,
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "files": bundle_records,
            "uploaded_by": self._user_alias,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_remote_manifest(manifest)
        registry.save_to_oss(self._bucket, self._prefix())

        logger.info(
            "[SkillHub] rolled back %s to v%d as new v%d", name, target, new_version
        )
        return {
            "name": name,
            "restored_from": target,
            "new_version": new_version,
            "bundle": bundle,
        }

    def sync_skills(self, skills_dir: str) -> dict[str, dict[str, Any]]:
        """Bidirectional sync: incremental pull (no deletes), then push."""
        pull_result = self.pull_skills(skills_dir, mirror=False)
        push_result = self.push_skills(skills_dir)
        return {"pull": pull_result, "push": push_result}
