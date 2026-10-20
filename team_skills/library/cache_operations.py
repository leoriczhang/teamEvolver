"""Rebuildable Skill runtime cache materialization."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from teamEvolver.storage import InMemoryObjectStore, LocalObjectStore, PgObjectStore

from . import layout
from .bundle import bundle_entrypoint_bytes, bundle_has_only_entrypoint, read_skill_bundle_with_meta, write_skill_bundle

logger = logging.getLogger(__name__)
_BACKUP_DIRNAME = ".teamEvolver_backups"
_STAGE_PREFIX = ".teamEvolver_pull_stage_"

class SkillCacheOperations:
    @staticmethod
    def _local_bundle_matches_record(skill_dir: str, record: dict[str, Any]) -> bool:
        bundle, _records, tree_sha = read_skill_bundle_with_meta(skill_dir)
        if not bundle:
            return False
        if record.get("format") == "skill-bundle":
            return str(record.get("tree_sha256") or "") == tree_sha

        try:
            skill_md = bundle_entrypoint_bytes(bundle)
        except Exception:
            return False
        skill_sha = hashlib.sha256(skill_md).hexdigest()
        return bundle_has_only_entrypoint(bundle) and str(record.get("sha256") or "") == skill_sha

    @staticmethod
    def _list_cached_skill_dirs(skills_dir: str) -> dict[str, list[str]]:
        """Return ``{skill_name: [skill_dir,...]}`` for cached Skill folders."""
        out: dict[str, list[str]] = {}
        if not os.path.isdir(skills_dir):
            return out
        for entry in os.scandir(skills_dir):
            if not entry.is_dir():
                continue
            if os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                out.setdefault(entry.name, []).append(entry.path)
        return out

    @staticmethod
    def _resolve_cache_target_dir(
        skills_dir: str,
        skill_name: str,
        category: str,
        local_dirs_by_name: dict[str, list[str]],
    ) -> str:
        """Choose the runtime-cache directory for a materialized Skill."""
        del local_dirs_by_name
        target = layout.skill_dir_for(skills_dir, skill_name, category)
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

    @staticmethod
    def _materialization_result(
        *,
        downloaded: int,
        skipped: int,
        deleted: int,
        total_remote: int,
        restored_from_backup: bool,
        backup_dir: str,
    ) -> dict[str, Any]:
        return {
            "downloaded": downloaded,
            "skipped": skipped,
            "deleted": deleted,
            "total_remote": total_remote,
            "restored_from_backup": restored_from_backup,
            "backup_dir": backup_dir,
        }

    def materialize_runtime_cache(
        self,
        runtime_cache_dir: str,
    ) -> dict[str, Any]:
        """Materialize stored Skills into a rebuildable runtime cache.

        The cache strictly mirrors the authoritative manifest. Materialization
        is staged and backed up so a failed download cannot expose a partial
        cache to file-based runtimes.
        """
        os.makedirs(runtime_cache_dir, exist_ok=True)
        cached_dirs = self._list_cached_skill_dirs(runtime_cache_dir)
        manifest = self._load_remote_manifest()
        return self._materialize_mirror(
            runtime_cache_dir,
            manifest,
            cached_dirs,
        )

    def _materialize_mirror(
        self,
        skills_dir: str,
        manifest: dict[str, dict[str, Any]],
        local_dirs_by_name: dict[str, list[str]],
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
            return self._materialization_result(
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
                target_dir = self._resolve_cache_target_dir(skills_dir, name, category, local_dirs_by_name)
                resolved_targets[name] = target_dir
                staged_dir = os.path.join(staging_dir, os.path.relpath(target_dir, skills_dir))

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
                logger.info("[SkillHub] materialized Skill: %s", name)

            remote_names = set(manifest.keys())
            local_names = set(local_skills.keys())
            for stale in sorted(local_names - remote_names):
                shutil.rmtree(local_skills[stale], ignore_errors=False)
                deleted += 1

            for name in sorted(remote_names):
                rec = manifest.get(name, {})
                category = str(rec.get("category", "general") or "general")
                dst_dir = resolved_targets.get(name) or self._resolve_cache_target_dir(
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
            return self._materialization_result(
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
            "[SkillHub] cache materialized: %d downloaded, %d skipped, %d deleted, %d total",
            downloaded,
            skipped,
            deleted,
            len(manifest),
        )
        self._prune_backups(backup_root, backup_prefix, keep=3)
        return self._materialization_result(
            downloaded=downloaded,
            skipped=skipped,
            deleted=deleted,
            total_remote=len(manifest),
            restored_from_backup=False,
            backup_dir=backup_dir,
        )

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
