"""Tenant-scoped Skill management backed exclusively by ``SkillHub``.

The configured PostgreSQL, OpenViking or NAS backend is authoritative. Local
files are only rebuildable runtime materializations for file-based Agent and
Replay runtimes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from team_skills.library import editor
from team_skills.library.bundle import (
    bundle_tree_sha256, candidate_skill_bundle, coerce_skill_bundle, diff_skill_bundles,
    is_ignored_bundle_rel_path, normalize_bundle_rel_path, read_skill_bundle,
    write_skill_bundle,
)
from team_skills.library.editor import SkillEditorError
from ..tenants.registry import DEFAULT_TENANT_ID, current_tenant_id

logger = logging.getLogger(__name__)

_VERSION_CONTEXT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_VERSION_DETAIL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _version_cache_key(name: str, suffix: str) -> str:
    """Version-cache key namespaced by the request's tenant (no-op default)."""
    tenant = current_tenant_id()
    prefix = "" if tenant == DEFAULT_TENANT_ID else f"t:{tenant}|"
    return f"{prefix}{name}:{suffix}"


def _clear_version_cache(name: str = "") -> None:
    for cache in (_VERSION_CONTEXT_CACHE, _VERSION_DETAIL_CACHE):
        for key in list(cache):
            # Keys may carry a "t:<tenant>|" prefix (multi-tenant scope);
            # compare the unprefixed tail so a purge covers every tenant.
            if not name or key.split("|")[-1].startswith(f"{name}:"):
                cache.pop(key, None)


def _decode_b64(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base64 in {field}: {e}") from e


def _require_admin_request(request: Request) -> None:
    from ..admin.authorization import require_tenant_admin
    user = getattr(request.state, "console_user", None)
    require_tenant_admin(user)


def _version_evolution_context(
    config,
    *,
    name: str,
    version: int,
    history: list[dict[str, Any]],
    store: Any = None,
    effective_config: Any = None,
) -> dict[str, Any]:
    cache_key = _version_cache_key(name, str(version))
    now = time.monotonic()
    cached = _VERSION_CONTEXT_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        from team_skills.candidates.store import ValidationStore
        from .routes import _candidate_list_payloads, _evaluation_payload

        if store is None:
            # Default-tenant/background callers pass the global config; console
            # requests pass the request-scoped tenant effective config so the
            # version context is read from the tenant's own validation store.
            store = ValidationStore.from_config(
                effective_config or config, tenant_id=current_tenant_id()
            )
        indexed = store.load_skill_version_context(name, version)
        if indexed:
            selected = {
                **(
                    indexed.get("job")
                    if isinstance(indexed.get("job"), dict)
                    else {}
                ),
                "decision": indexed.get("decision") or {},
                "evaluation": indexed.get("evaluation") or {},
                "decided_at": indexed.get("decided_at") or "",
                "review_status": str(
                    (indexed.get("decision") or {}).get("status") or ""
                ),
            }
        else:
            selected = None
        candidates = _candidate_list_payloads(store, scope="processed")
    except Exception:  # noqa: BLE001 - version content remains available.
        selected = None
        candidates = []

    target_history = next(
        (
            item
            for item in history
            if int(item.get("version") or 0) == int(version)
        ),
        {},
    )
    target_timestamp = str(target_history.get("timestamp") or "")

    def timestamp_distance(candidate: dict[str, Any]) -> float:
        decided = str(candidate.get("decided_at") or "")
        if not decided or not target_timestamp:
            return float("inf")
        try:
            from datetime import datetime

            return abs(
                (
                    datetime.fromisoformat(decided.replace("Z", "+00:00"))
                    - datetime.fromisoformat(target_timestamp.replace("Z", "+00:00"))
                ).total_seconds()
            )
        except (TypeError, ValueError):
            return float("inf")

    if selected is None:
        matching = [
            item
            for item in candidates
            if str(item.get("skill_name") or "") == name
            and str(item.get("review_status") or "") == "published"
        ]
        exact = [
            item
            for item in matching
            if int((item.get("decision") or {}).get("version") or 0) == int(version)
        ]
        selected = exact[0] if exact else (
            min(matching, key=timestamp_distance) if matching else None
        )
        if selected is not None and not exact and timestamp_distance(selected) > 300:
            selected = None

    context: dict[str, Any] = {}
    if selected:
        candidate_skill = (
            selected.get("candidate_skill")
            if isinstance(selected.get("candidate_skill"), dict)
            else {}
        )
        current_skill = (
            selected.get("current_skill")
            if isinstance(selected.get("current_skill"), dict)
            else {}
        )
        edit_summary = (
            candidate_skill.get("edit_summary")
            if isinstance(candidate_skill.get("edit_summary"), dict)
            else {}
        )
        optimization_items: list[str] = []
        for item in edit_summary.get("changed_sections") or []:
            text = str(item or "").strip()
            if text and text not in optimization_items:
                optimization_items.append(text)
        notes = str(edit_summary.get("notes") or "").strip()
        if notes and notes not in optimization_items:
            optimization_items.append(notes)
        evidence = (
            selected.get("evidence_classification")
            if isinstance(selected.get("evidence_classification"), dict)
            else {}
        )
        for item in evidence.get("team_skill") or []:
            text = str(item.get("claim") if isinstance(item, dict) else item).strip()
            if text and text not in optimization_items:
                optimization_items.append(text)
        for change in candidate_skill.get("file_changes") or []:
            if not isinstance(change, dict):
                continue
            text = (
                f"{change.get('operation')}: {change.get('path')} - "
                f"{change.get('reason') or ''}"
            ).strip()
            if text not in optimization_items:
                optimization_items.append(text)
        bundle_diff = diff_skill_bundles(
            candidate_skill_bundle(current_skill) if current_skill else {},
            candidate_skill_bundle(candidate_skill),
        )
        skill_file = next(
            (
                item
                for item in bundle_diff["files"]
                if item.get("path") == "SKILL.md"
            ),
            {},
        )
        raw_evaluation = (
            selected.get("evaluation")
            if isinstance(selected.get("evaluation"), dict)
            else {}
        )
        normalized_evaluation = (
            raw_evaluation
            if isinstance(raw_evaluation.get("replay"), dict)
            else _evaluation_payload(selected, raw_evaluation, cached=True)
        )
        context = {
            "job_id": str(selected.get("job_id") or ""),
            "proposed_action": str(selected.get("proposed_action") or ""),
            "rationale": str(selected.get("rationale") or ""),
            "edit_summary": edit_summary,
            "optimization_items": optimization_items,
            "evidence_classification": evidence,
            "decision": selected.get("decision") or {},
            "evaluation": normalized_evaluation,
            "skill_diff": str(skill_file.get("diff") or ""),
            "bundle_diff": bundle_diff,
            "static_validation": candidate_skill.get("static_validation"),
        }
    _VERSION_CONTEXT_CACHE[cache_key] = (now + 30.0, context)
    return context


class SkillsAdminMixin:
    """CRUD, transfer and runtime-cache routes for the tenant Skill registry."""

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _skill_runtime_cache_dir(self) -> str:
        base = os.path.expanduser(
            str(
                getattr(self.config, "skills_runtime_cache_dir", "")
                or "~/.teamEvolver/runtime/skills"
            )
        )
        tenant = current_tenant_id()
        if tenant and tenant != DEFAULT_TENANT_ID:
            return os.path.join(base, "tenants", tenant)
        return base

    def _materialize_runtime_bundle(
        self,
        name: str,
        bundle: dict[str, bytes],
        *,
        category: str = "general",
    ) -> dict[str, Any]:
        """Refresh one rebuildable runtime-cache entry after a durable commit."""
        try:
            from team_skills.library import layout

            cache_root = self._skill_runtime_cache_dir()
            target = layout.skill_dir_for(
                cache_root,
                name,
                category,
            )
            previous = editor.find_skill_dir(cache_root, name)
            if previous and os.path.realpath(previous) != os.path.realpath(target):
                shutil.rmtree(previous)
            write_skill_bundle(target, bundle, clean=True)
            return {"materialized": True}
        except Exception as exc:  # durable publication already succeeded
            logger.warning(
                "[SkillsAdmin] runtime cache refresh failed for %s: %s",
                name,
                exc,
            )
            return {"materialized": False, "reason": str(exc)}

    def _remove_runtime_bundle(self, name: str) -> dict[str, Any]:
        """Remove one tenant cache entry without affecting durable storage."""
        try:
            path = editor.find_skill_dir(self._skill_runtime_cache_dir(), name)
            if path:
                shutil.rmtree(path)
            return {"materialized": False}
        except Exception as exc:
            logger.warning(
                "[SkillsAdmin] runtime cache removal failed for %s: %s",
                name,
                exc,
            )
            return {"materialized": True, "reason": str(exc)}

    def _sync_bundle_payload(self, config: Any = None) -> dict[str, Any]:
        """Return a read-only team skill snapshot for lightweight agents.

        Agents can sync from teamEvolver itself instead of duplicating
        OpenViking credentials and root-prefix knowledge locally.
        The service remains the only place that needs object-storage config.
        """
        config = config or self.config
        try:
            from team_skills.library.hub import SkillHub

            hub = SkillHub.team_from_config(config, tenant_id=current_tenant_id())
            bundles: list[dict[str, Any]] = []
            for record in hub.list_skills():
                name = str(record.get("name") or "")
                if not name:
                    continue
                try:
                    version = int(record.get("version") or 0)
                except (TypeError, ValueError):
                    version = 0
                bundle = (
                    hub.read_version_bundle(name, version)
                    if version > 0
                    else hub.read_skill_bundle(name)
                )
                bundle = coerce_skill_bundle(bundle)
                files = [
                    {
                        "path": rel_path,
                        "content_b64": base64.b64encode(content).decode("ascii"),
                    }
                    for rel_path, content in sorted(bundle.items())
                ]
                bundles.append({
                    **record, "files": files,
                    "sha256": hashlib.sha256(bundle.get("SKILL.md", b"")).hexdigest(),
                    "tree_sha256": bundle_tree_sha256(bundle),
                })
            return {
                "status": "ok",
                "source": "registry",
                "skills": sorted(bundles, key=lambda item: item["name"]),
                "total": len(bundles),
            }
        except Exception as e:  # noqa: BLE001 - sync endpoint should explain failure
            logger.warning("[SkillsAdmin] registry sync snapshot failed: %s", e)
            return {
                "status": "error",
                "source": "registry",
                "error": str(e),
                "skills": [],
                "total": 0,
            }

    # ------------------------------------------------------------------ #
    # Route registration                                                 #
    # ------------------------------------------------------------------ #

    def _register_skills_admin_routes(self, app: FastAPI) -> None:
        owner = self
        from .skill_transfer import register_skill_transfer_routes

        register_skill_transfer_routes(app, owner)

        def _eff_config():
            """Request-scoped effective config (tenant overrides merged)."""
            from .routes import _tenant_effective_config

            return _tenant_effective_config(owner)

        def _hub():
            from team_skills.library.hub import SkillHub

            return SkillHub.team_from_config(
                _eff_config(),
                tenant_id=current_tenant_id(),
            )

        def _mutation_service(hub=None):
            from team_skills.library.mutations import SkillMutationService

            return SkillMutationService.from_hub(
                hub or _hub(),
                config=_eff_config(),
            )

        def _publish_bundle(
            name: str,
            bundle: dict[str, bytes],
            *,
            action: str,
            mutation_id: str,
            hub=None,
        ) -> dict[str, Any]:
            hub = hub or _hub()
            commit = _mutation_service(hub).publish_bundle(
                action=action,
                name=name,
                mutation_id=mutation_id,
                bundle=bundle,
            )
            record = dict(commit.get("expected") or {})
            cache = owner._materialize_runtime_bundle(
                name,
                bundle,
                category=str(record.get("category") or "general"),
            )
            return {
                "name": name,
                "version": int(record.get("version") or 0),
                "files": sorted(bundle),
                "file_count": len(bundle),
                "event_id": str(commit.get("event_id") or ""),
                "runtime_cache": cache,
            }

        @app.get("/api/skills")
        async def api_list_skills():
            config = _eff_config()
            backend = str(
                getattr(config, "sharing_skill_backend", "")
                or ("postgres" if getattr(config, "storage_pg_enabled", False) else "local")
            ).strip().lower()
            records = await asyncio.to_thread(_hub().list_skills)
            skills = [
                {
                    **record,
                    "file_count": len(record.get("files") or []),
                    "updated_at": str(record.get("uploaded_at") or ""),
                }
                for record in records
            ]
            return JSONResponse(
                content={
                    "skill_backend": backend,
                    "delivery_mode": "pull",
                    "skills": skills,
                }
            )

        @app.get("/api/skills/{name}")
        async def api_get_skill(name: str):
            try:
                def load():
                    hub = _hub()
                    info = hub.list_versions(name)
                    cv = int(info.get("current_version") or 0)
                    if cv <= 0:
                        raise FileNotFoundError(name)
                    detail = hub.get_version_detail(name, cv)
                    raw_md = detail.get("raw_md") or ""
                    files = [
                        str(item.get("path") or "")
                        for item in detail.get("files") or []
                        if isinstance(item, dict) and str(item.get("path") or "")
                    ]
                    return {
                        "name": name,
                        "description": detail.get("description") or "",
                        "category": detail.get("category") or "general",
                        "body": detail.get("content") or "",
                        "skill_md": raw_md,
                        "files": files,
                        "file_count": len(files),
                        "updated_at": "",
                    }

                return JSONResponse(content=await asyncio.to_thread(load))
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=f"Skill not found: {name}") from exc
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Skill storage unavailable: {exc}") from exc

        @app.get("/api/skills/{name}/versions")
        async def api_list_skill_versions(name: str):
            """List a skill's version history from the shared cloud registry."""
            try:
                from team_skills.library.hub import SkillHub

                cache_key = _version_cache_key(name, "list")
                now = time.monotonic()
                cached = _VERSION_DETAIL_CACHE.get(cache_key)
                if cached and cached[0] > now:
                    return JSONResponse(content=cached[1])
                payload = SkillHub.team_from_config(
                    _eff_config(), tenant_id=current_tenant_id()
                ).list_versions(name)
                _VERSION_DETAIL_CACHE[cache_key] = (now + 15.0, payload)
                return JSONResponse(content=payload)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=404, detail=f"versions unavailable: {e}") from e

        @app.get("/api/skills/{name}/versions/{version}")
        async def api_get_skill_version(name: str, version: int):
            """Return one version's SKILL.md content + parsed metadata."""
            try:
                from team_skills.library.hub import SkillHub

                eff_cfg = _eff_config()
                hub = SkillHub.team_from_config(eff_cfg, tenant_id=current_tenant_id())
                cache_key = _version_cache_key(name, str(int(version)))
                now = time.monotonic()
                cached = _VERSION_DETAIL_CACHE.get(cache_key)
                if cached and cached[0] > now:
                    return JSONResponse(content=cached[1])
                payload = hub.get_version_detail(name, int(version))
                history = hub.list_versions(name).get("history") or []
                payload["evolution"] = _version_evolution_context(
                    owner.config,
                    name=name,
                    version=int(version),
                    history=history,
                    effective_config=eff_cfg,
                )
                _VERSION_DETAIL_CACHE[cache_key] = (now + 30.0, payload)
                return JSONResponse(content=payload)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=404, detail=f"version unavailable: {e}") from e

        @app.post("/api/skills/{name}/rollback")
        async def api_rollback_skill(name: str, target_version: int, request: Request):
            """Republish ``target_version``'s content as a new current version.

            The durable registry commits first; the runtime cache is refreshed
            only after the new version is visible from SkillHub.
            """
            _require_admin_request(request)
            try:
                def rollback():
                    from team_skills.library.mutations import SkillMutationCommand

                    hub = _hub()
                    commit = _mutation_service(hub).execute(
                        SkillMutationCommand(
                            action="rollback",
                            name=name,
                            mutation_id=f"admin-rollback-{uuid.uuid4().hex}",
                            target_version=int(target_version),
                        )
                    )
                    result = dict(commit.get("result") or {})
                    new_version = int(result.get("new_version") or 0)
                    bundle = hub.read_version_bundle(name, new_version)
                    record = dict(commit.get("expected") or {})
                    cache = owner._materialize_runtime_bundle(
                        name,
                        bundle,
                        category=str(record.get("category") or "general"),
                    )
                    return {
                        **result,
                        "event_id": commit.get("event_id") or "",
                        "runtime_cache": cache,
                    }

                payload = await asyncio.to_thread(rollback)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"rollback failed: {e}") from e

            _clear_version_cache(name)
            return JSONResponse(content=payload)

        @app.get("/sync/skills")
        async def sync_skills_snapshot(request: Request, user_id: str = ""):
            from ..integrations.agent_principal import resolve_agent_principal

            config = _eff_config()
            principal = resolve_agent_principal(request, config, user_id)
            payload = await asyncio.to_thread(owner._sync_bundle_payload, config)
            if payload.get("status") != "ok":
                return JSONResponse(status_code=503, content=payload)
            canonical = json.dumps(
                [principal.tenant_id, principal.account_id, payload], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            etag = '"' + hashlib.sha256(canonical).hexdigest() + '"'
            headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
            validators = {value.strip().removeprefix("W/") for value in request.headers.get("if-none-match", "").split(",")}
            if etag in validators or "*" in validators:
                return Response(status_code=304, headers=headers)
            return JSONResponse(content=payload, headers=headers)

        @app.post("/api/skills")
        async def api_create_or_update_skill(body: dict[str, Any], request: Request):
            """Create or overwrite a skill's SKILL.md from structured fields.

            Body: ``{name, description, category, body, skill_md?}``. When
            ``skill_md`` is present it is written verbatim (raw edit mode).
            """
            _require_admin_request(request)
            try:
                def save():
                    name = editor.validate_skill_name(str(body.get("name", "")))
                    hub = _hub()
                    try:
                        current = hub.read_skill_bundle(name)
                    except FileNotFoundError:
                        current = {}
                    created = not current
                    with tempfile.TemporaryDirectory(prefix="te-skill-edit-") as tmp:
                        if current:
                            write_skill_bundle(Path(tmp) / name, current)
                        edited = editor.save_skill(
                            tmp,
                            name=name,
                            description=str(body.get("description", "")),
                            category=str(body.get("category", "") or "general"),
                            body=str(body.get("body", "")),
                            skill_md=str(body.get("skill_md", "") or ""),
                        )
                        bundle = read_skill_bundle(edited["dir"])
                    published = _publish_bundle(
                        name,
                        bundle,
                        action="publish" if created else "update",
                        mutation_id=f"admin-save-{uuid.uuid4().hex}",
                        hub=hub,
                    )
                    return {**published, "created": created}

                result = await asyncio.to_thread(save)
            except (SkillEditorError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Skill publication failed: {e}") from e
            _clear_version_cache(result["name"])
            return JSONResponse(content=result)

        @app.delete("/api/skills/{name}")
        async def api_delete_skill(name: str, request: Request):
            _require_admin_request(request)
            try:
                def delete():
                    clean = editor.validate_skill_name(name)
                    hub = _hub()
                    hub.read_skill_bundle(clean)
                    from team_skills.library.mutations import SkillMutationCommand

                    commit = _mutation_service(hub).execute(
                        SkillMutationCommand(
                            action="delete",
                            name=clean,
                            mutation_id=f"admin-delete-{uuid.uuid4().hex}",
                        )
                    )
                    return {
                        "name": clean,
                        "deleted": True,
                        "event_id": str(commit.get("event_id") or ""),
                        "runtime_cache": owner._remove_runtime_bundle(clean),
                    }

                result = await asyncio.to_thread(delete)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except (SkillEditorError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Skill deletion failed: {e}") from e
            _clear_version_cache(result["name"])
            return JSONResponse(content=result)

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
                def add_files():
                    clean_name = editor.validate_skill_name(name)
                    hub = _hub()
                    bundle = hub.read_skill_bundle(clean_name)
                    written: list[str] = []
                    for raw_path, content in payload.items():
                        rel_path = normalize_bundle_rel_path(raw_path)
                        if rel_path == "SKILL.md":
                            raise SkillEditorError(
                                "Use the editor to change SKILL.md, not file upload"
                            )
                        if is_ignored_bundle_rel_path(rel_path):
                            continue
                        bundle[rel_path] = content
                        written.append(rel_path)
                    result = _publish_bundle(
                        clean_name,
                        bundle,
                        action="update",
                        mutation_id=f"admin-files-{uuid.uuid4().hex}",
                        hub=hub,
                    )
                    return {**result, "written": sorted(written)}

                result = await asyncio.to_thread(add_files)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except (SkillEditorError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Skill publication failed: {e}") from e
            _clear_version_cache(result["name"])
            return JSONResponse(content=result)

        @app.delete("/api/skills/{name}/files/{rel_path:path}")
        async def api_delete_file(name: str, rel_path: str, request: Request):
            _require_admin_request(request)
            try:
                def delete_file():
                    clean_name = editor.validate_skill_name(name)
                    clean_path = normalize_bundle_rel_path(rel_path)
                    if clean_path == "SKILL.md":
                        raise SkillEditorError(
                            "SKILL.md cannot be deleted; delete the whole skill instead"
                        )
                    hub = _hub()
                    bundle = hub.read_skill_bundle(clean_name)
                    if clean_path not in bundle:
                        raise FileNotFoundError(f"{clean_name}/{clean_path} not found")
                    bundle.pop(clean_path)
                    result = _publish_bundle(
                        clean_name,
                        bundle,
                        action="update",
                        mutation_id=f"admin-files-{uuid.uuid4().hex}",
                        hub=hub,
                    )
                    return {**result, "removed": clean_path}

                result = await asyncio.to_thread(delete_file)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except (SkillEditorError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Skill publication failed: {e}") from e
            _clear_version_cache(result["name"])
            return JSONResponse(content=result)
