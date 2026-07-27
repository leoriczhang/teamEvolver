"""Skill management REST API for the SkillGene service.

``SkillsAdminMixin`` exposes CRUD + upload endpoints over the local skill
library (``config.skills_dir``) plus the single-file management UI. Every
mutation optionally auto-syncs to the team-shared cloud (OpenViking / local
object storage) via :class:`~skillgene.skills.hub.SkillHub` and reloads the
running :class:`~skillgene.skills.manager.SkillManager` so injected skills stay
current without a restart.

Routes are intentionally local-management endpoints so the operator can manage
skills from the authenticated console. Do not expose the service port publicly
unless it is protected by your deployment boundary.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..skills import editor
from ..skills.editor import SkillEditorError

logger = logging.getLogger(__name__)

_SKILLS_UI_PATH = Path(__file__).resolve().parent.parent / "web" / "skills.html"


def _decode_b64(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base64 in {field}: {e}") from e


def _require_admin_request(request: Request) -> None:
    user = getattr(request.state, "console_user", None)
    if not isinstance(user, dict) or str(user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")


class SkillsAdminMixin:
    """CRUD, upload, and cloud-sync routes for the local skill library."""

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _skills_dir(self) -> str:
        return str(getattr(self.config, "skills_dir", "") or "")

    def _reload_skill_manager(self) -> int:
        """Reload the manager so edits show up in injection immediately."""
        if self.skill_manager is None:
            return 0
        try:
            self.skill_manager.reload()
            self.skill_manager.generation += 1
        except Exception as e:  # noqa: BLE001 - reload must never 5xx an edit
            logger.warning("[SkillsAdmin] skill manager reload failed: %s", e)
            return 0
        return len(self.skill_manager.get_all_skills())

    def _cloud_sync_push(self, name: str) -> dict[str, Any]:
        """Push a single skill to the shared cloud; never raises."""
        if not getattr(self.config, "sharing_enabled", False):
            return {"synced": False, "reason": "sharing_disabled"}
        try:
            from ..skills.hub import SkillHub

            hub = SkillHub.team_from_config(self.config)
            result = hub.push_skills(self._skills_dir(), include_names=[name])
            return {"synced": True, "action": "push", **result}
        except Exception as e:  # noqa: BLE001 - cloud errors are advisory
            logger.warning("[SkillsAdmin] cloud push failed for %s: %s", name, e)
            return {"synced": False, "reason": str(e)}

    def _cloud_sync_delete(self, name: str) -> dict[str, Any]:
        """Delete a single skill from the shared cloud; never raises."""
        if not getattr(self.config, "sharing_enabled", False):
            return {"synced": False, "reason": "sharing_disabled"}
        try:
            from ..skills.hub import SkillHub

            hub = SkillHub.team_from_config(self.config)
            result = hub.delete_skill(name)
            return {"synced": True, "action": "delete", **result}
        except Exception as e:  # noqa: BLE001 - cloud errors are advisory
            logger.warning("[SkillsAdmin] cloud delete failed for %s: %s", name, e)
            return {"synced": False, "reason": str(e)}

    def _sync_bundle_payload(self) -> dict[str, Any]:
        """Return a read-only team skill snapshot for lightweight agents.

        This endpoint lets Hermes machines sync from SkillGene itself instead
        of duplicating OpenViking credentials and root-prefix knowledge locally.
        The service remains the only place that needs object-storage config.
        """
        if getattr(self.config, "sharing_enabled", False):
            try:
                from ..skills.hub import SkillHub

                hub = SkillHub.team_from_config(self.config)
                bundles: list[dict[str, Any]] = []
                for record in hub.list_remote():
                    name = str(record.get("name") or "")
                    if not name:
                        continue
                    bundle = hub._download_skill_bundle(name, record)
                    files = [
                        {
                            "path": rel_path,
                            "content_b64": base64.b64encode(content).decode("ascii"),
                        }
                        for rel_path, content in sorted(bundle.items())
                    ]
                    bundles.append({**record, "files": files})
                return {
                    "status": "ok",
                    "source": "shared",
                    "skills": bundles,
                    "total": len(bundles),
                }
            except Exception as e:  # noqa: BLE001 - sync endpoint should explain failure
                logger.warning("[SkillsAdmin] shared sync snapshot failed: %s", e)
                return {
                    "status": "error",
                    "source": "shared",
                    "error": str(e),
                    "skills": [],
                    "total": 0,
                }

        skills_dir = self._skills_dir()
        bundles: list[dict[str, Any]] = []
        for summary in editor.list_skills(skills_dir):
            name = str(summary.get("name") or "")
            if not name:
                continue
            detail = editor.get_skill(skills_dir, name)
            skill_dir = editor.find_skill_dir(skills_dir, name)
            files: list[dict[str, Any]] = []
            for rel_path in detail.get("files") or []:
                rel = str(rel_path or "").strip().replace("\\", "/")
                if not rel or rel.startswith("../") or rel.startswith("/"):
                    continue
                file_path = os.path.join(skill_dir or "", rel)
                if not os.path.isfile(file_path):
                    continue
                with open(file_path, "rb") as handle:
                    content = handle.read()
                files.append(
                    {
                        "path": rel,
                        "content_b64": base64.b64encode(content).decode("ascii"),
                    }
                )
            bundles.append({**summary, "files": files})
        return {"status": "ok", "source": "local", "skills": bundles, "total": len(bundles)}

    # ------------------------------------------------------------------ #
    # Route registration                                                 #
    # ------------------------------------------------------------------ #

    def _register_skills_admin_routes(self, app: FastAPI) -> None:
        owner = self

        @app.get("/skills-ui", response_class=HTMLResponse)
        async def skills_ui():
            """Serve the single-file skill management UI."""
            try:
                return HTMLResponse(content=_SKILLS_UI_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return HTMLResponse(content="<h1>skills.html not found</h1>", status_code=404)

        @app.get("/api/skills")
        async def api_list_skills():
            return JSONResponse(
                content={
                    "sharing_enabled": bool(getattr(owner.config, "sharing_enabled", False)),
                    "skills": editor.list_skills(owner._skills_dir()),
                }
            )

        @app.get("/api/skills/{name}")
        async def api_get_skill(name: str):
            try:
                return JSONResponse(content=editor.get_skill(owner._skills_dir(), name))
            except SkillEditorError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e

        @app.get("/api/skills/{name}/versions")
        async def api_list_skill_versions(name: str):
            """List a skill's version history from the shared cloud registry."""
            try:
                from ..skills.hub import SkillHub

                hub = SkillHub.team_from_config(owner.config)
                return JSONResponse(content=hub.list_versions(name))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=404, detail=f"versions unavailable: {e}") from e

        @app.get("/api/skills/{name}/versions/{version}")
        async def api_get_skill_version(name: str, version: int):
            """Return one version's SKILL.md content + parsed metadata."""
            try:
                from ..skills.hub import SkillHub

                hub = SkillHub.team_from_config(owner.config)
                return JSONResponse(content=hub.get_version_detail(name, int(version)))
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=404, detail=f"version unavailable: {e}") from e

        @app.post("/api/skills/{name}/rollback")
        async def api_rollback_skill(name: str, target_version: int, request: Request):
            """Republish ``target_version``'s content as a new current version.

            Admin-only. Rolls the shared cloud copy back, then mirrors the
            restored bundle into the local skills dir and reloads the manager so
            injection immediately reflects the rollback.
            """
            _require_admin_request(request)
            try:
                from ..skills.hub import SkillHub
                from ..skills.bundle import write_skill_bundle
                from ..skills import layout

                hub = SkillHub.team_from_config(owner.config)
                result = hub.rollback_skill(name, int(target_version))
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"rollback failed: {e}") from e

            bundle = result.pop("bundle", None)
            if isinstance(bundle, dict) and bundle:
                try:
                    detail = hub.get_version_detail(name, int(result.get("new_version") or 0))
                    category = str(detail.get("category") or "general")
                    local_dir = layout.skill_dir_for(owner._skills_dir(), name, category)
                    write_skill_bundle(local_dir, bundle, clean=True)
                except Exception as e:  # noqa: BLE001 - local mirror is best-effort
                    logger.warning("[SkillsAdmin] rollback local mirror failed for %s: %s", name, e)
            loaded = owner._reload_skill_manager()
            return JSONResponse(content={**result, "loaded_skills": loaded})

        @app.get("/sync/skills")
        async def sync_skills_snapshot():
            return JSONResponse(content=owner._sync_bundle_payload())

        @app.post("/api/skills")
        async def api_create_or_update_skill(body: dict[str, Any], request: Request):
            """Create or overwrite a skill's SKILL.md from structured fields.

            Body: ``{name, description, category, body, skill_md?}``. When
            ``skill_md`` is present it is written verbatim (raw edit mode).
            """
            _require_admin_request(request)
            try:
                result = editor.save_skill(
                    owner._skills_dir(),
                    name=str(body.get("name", "")),
                    description=str(body.get("description", "")),
                    category=str(body.get("category", "") or "general"),
                    body=str(body.get("body", "")),
                    skill_md=str(body.get("skill_md", "") or ""),
                )
            except SkillEditorError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            loaded = owner._reload_skill_manager()
            sync = owner._cloud_sync_push(result["name"])
            return JSONResponse(content={**result, "loaded_skills": loaded, "cloud": sync})

        @app.delete("/api/skills/{name}")
        async def api_delete_skill(name: str, request: Request):
            _require_admin_request(request)
            try:
                result = editor.delete_skill(owner._skills_dir(), name)
            except SkillEditorError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            loaded = owner._reload_skill_manager()
            sync = owner._cloud_sync_delete(result["name"])
            return JSONResponse(content={**result, "loaded_skills": loaded, "cloud": sync})

        @app.post("/api/skills/{name}/files")
        async def api_add_files(name: str, body: dict[str, Any], request: Request):
            """Add/replace bundle files under a skill.

            Body: ``{files: [{path, content_b64}, ...]}``.
            """
            _require_admin_request(request)
            entries = body.get("files")
            if not isinstance(entries, list) or not entries:
                raise HTTPException(status_code=400, detail="files must be a non-empty list")
            payload: dict[str, bytes] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                rel = str(item.get("path", "")).strip()
                if not rel:
                    raise HTTPException(status_code=400, detail="each file needs a path")
                payload[rel] = _decode_b64(item.get("content_b64", ""), field=rel)
            try:
                result = editor.add_bundle_files(owner._skills_dir(), name, payload)
            except SkillEditorError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            loaded = owner._reload_skill_manager()
            sync = owner._cloud_sync_push(result["name"])
            return JSONResponse(content={**result, "loaded_skills": loaded, "cloud": sync})

        @app.delete("/api/skills/{name}/files/{rel_path:path}")
        async def api_delete_file(name: str, rel_path: str, request: Request):
            _require_admin_request(request)
            try:
                result = editor.delete_bundle_file(owner._skills_dir(), name, rel_path)
            except SkillEditorError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            loaded = owner._reload_skill_manager()
            sync = owner._cloud_sync_push(result["name"])
            return JSONResponse(content={**result, "loaded_skills": loaded, "cloud": sync})

        @app.post("/api/skills/import-zip")
        async def api_import_zip(body: dict[str, Any], request: Request):
            """Import a zipped skill package.

            Body: ``{zip_b64, name?}``.
            """
            _require_admin_request(request)
            zip_bytes = _decode_b64(body.get("zip_b64", ""), field="zip_b64")
            if not zip_bytes:
                raise HTTPException(status_code=400, detail="zip_b64 must not be empty")
            try:
                result = editor.import_zip(
                    owner._skills_dir(),
                    zip_bytes,
                    name_override=str(body.get("name", "") or ""),
                )
            except SkillEditorError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            loaded = owner._reload_skill_manager()
            sync = owner._cloud_sync_push(result["name"])
            return JSONResponse(content={**result, "loaded_skills": loaded, "cloud": sync})
