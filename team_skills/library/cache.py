"""Rebuildable tenant Skill runtime cache.

PostgreSQL remains the authority. A missing or failed local cache cannot fail a
committed mutation or become the only copy of a Skill."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from team_skills.library.bundle import write_skill_bundle
from team_skills.library.editor import validate_skill_name

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "default"
_STAGE_PREFIX = ".te-cache-stage-"


class SkillRuntimeCache:
    """Best-effort materialiser of committed Skill bundles into a local dir."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(os.path.expanduser(str(root)))

    def tenant_dir(self, tenant_id: str) -> Path:
        tenant = str(tenant_id or DEFAULT_TENANT_ID)
        if tenant == DEFAULT_TENANT_ID:
            return self._root
        validate_skill_name(tenant)  # same traversal-safe charset as tenant ids
        return self._root / "tenants" / tenant

    def _swap_in(self, base: Path, name: str, bundle: Mapping[str, bytes]) -> None:
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=base))
        try:
            write_skill_bundle(stage / name, dict(bundle), clean=True)
            target = base / name
            retired = base / f"{_STAGE_PREFIX}old-{name}"
            if target.exists():
                if retired.exists():
                    shutil.rmtree(retired)
                os.replace(target, retired)
            os.replace(stage / name, target)
            if retired.exists():
                shutil.rmtree(retired, ignore_errors=True)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def materialize(self, tenant_id: str, name: str, bundle: Mapping[str, bytes]) -> dict[str, Any]:
        """Refresh one skill's cache entry after a durable commit (never raises)."""
        try:
            name = validate_skill_name(name)
            self._swap_in(self.tenant_dir(tenant_id), name, bundle)
            return {"materialized": True}
        except Exception as exc:  # noqa: BLE001 - the PG commit already succeeded
            logger.warning("[SkillRuntimeCache] refresh failed tenant=%s skill=%s: %s", tenant_id, name, exc)
            return {"materialized": False, "reason": f"{type(exc).__name__}: {exc}"}

    def remove(self, tenant_id: str, name: str) -> dict[str, Any]:
        """Drop one skill's cache entry (never raises)."""
        try:
            name = validate_skill_name(name)
            target = self.tenant_dir(tenant_id) / name
            if target.exists():
                shutil.rmtree(target)
            return {"materialized": False}
        except Exception as exc:  # noqa: BLE001 - the PG commit already succeeded
            logger.warning("[SkillRuntimeCache] removal failed tenant=%s skill=%s: %s", tenant_id, name, exc)
            return {"materialized": True, "reason": f"{type(exc).__name__}: {exc}"}

    def rebuild(self, tenant_id: str, bundles: Iterable[tuple[str, Mapping[str, bytes]]]) -> dict[str, Any]:
        """Make the tenant cache mirror exactly ``bundles`` (stale entries removed)."""
        base = self.tenant_dir(tenant_id)
        written: list[str] = []
        failed: list[str] = []
        keep: set[str] = set()
        for name, bundle in bundles:
            keep.add(name)
            result = self.materialize(tenant_id, name, bundle)
            (written if result["materialized"] else failed).append(name)
        removed = 0
        if base.is_dir():
            for entry in base.iterdir():
                if entry.name in keep or entry.name == "tenants" or entry.name.startswith("."):
                    continue
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
        return {"materialized": len(written), "failed": failed, "removed": removed}


__all__ = ["SkillRuntimeCache"]
