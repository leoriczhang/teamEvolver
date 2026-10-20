"""PostgreSQL Skill library, candidates, experiences, transfer and Workbench APIs.

All authoritative reads use PostgreSQL. Mutations validate the bundle and write
immutable versions atomically; an Idempotency-Key replays the first committed
result. Rebuildable runtime caches refresh after commit and cache failures do
not invalidate the committed version. Agents obtain snapshots by pull."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from teamEvolver.tenants.registry import current_tenant_id

from ..config import load_skills_config
from ..evolution.history import version_context
from ..library import frontmatter
from ..library.bundle import bundle_tree_sha256, normalize_bundle_rel_path
from ..library.editor import validate_skill_name
from ..library.repository import SkillLibraryError, SkillLibraryRepository, SkillNotFound

_MAX_JSON_BODY_BYTES = 96 * 1024 * 1024


def create_router(services=None, **kwargs) -> APIRouter:
    """Return this module's ``APIRouter`` for the composition root to mount."""
    router = APIRouter(tags=["skills"])
    extras = getattr(services, "extras", None) or {}
    config_repo = extras.get("config_repo")
    if services is None or config_repo is None:
        return router

    ctx = _SkillsContext(
        tx=services.tx,
        library=SkillLibraryRepository(services.tx),
        config_repo=config_repo,
        cache=_runtime_cache(extras),
        config=extras.get("config"),
    )
    from .transfer import create_router as transfer_router

    router.include_router(transfer_router(services))
    _register_library_reads(router, ctx)
    _register_library_writes(router, ctx)
    _register_agent_pull(router, ctx)
    from .candidates import create_router as candidate_router
    from .experiences import create_router as experience_router

    router.include_router(candidate_router(services))
    router.include_router(experience_router(services))
    from .workbench import create_router as workbench_router

    router.include_router(workbench_router(services))
    from .prompts import create_router as prompts_router
    router.include_router(prompts_router(services))
    return router


def _runtime_cache(extras: dict[str, Any]):
    """The injected cache, or one built from ``skill_runtime_cache_dir``."""
    cache = extras.get("skill_runtime_cache")
    if cache is not None:
        return cache
    cache_dir = str(extras.get("skill_runtime_cache_dir") or "").strip()
    if not cache_dir:
        return None
    from ..library.cache import SkillRuntimeCache

    return SkillRuntimeCache(cache_dir)


class _SkillsContext:
    """Collaborators shared by the route groups of one router instance."""

    def __init__(self, *, tx, library: SkillLibraryRepository, config_repo, cache, config) -> None:
        self.tx = tx
        self.library = library
        self.config_repo = config_repo
        self.cache = cache
        self.config = config

    async def limits(self, conn):
        from teamEvolver.config_runtime import TenantConfigMissing

        try:
            return (await load_skills_config(self.config_repo, conn)).bundle_limits
        except TenantConfigMissing as exc:
            # Fail closed with an explicit reason (no YAML / local fallback).
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def refresh_cache(self, tenant_id: str, name: str, bundle: dict[str, bytes] | None) -> dict[str, Any]:
        """Best-effort runtime-cache refresh after a committed mutation."""
        if self.cache is None:
            return {"materialized": False, "reason": "runtime cache disabled"}
        if bundle is None:
            return self.cache.remove(tenant_id, name)
        return self.cache.materialize(tenant_id, name, bundle)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _http_error(exc: SkillLibraryError) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 400), detail=str(exc))


def _require_tenant_admin(request: Request) -> None:
    from teamEvolver.admin.authorization import require_tenant_admin

    require_tenant_admin(getattr(request.state, "console_user", None))


def _actor(request: Request) -> str:
    user = getattr(request.state, "console_user", None) or {}
    return str(user.get("id") or user.get("username") or "")


def _mutation_id(request: Request, prefix: str) -> str:
    """Caller-supplied ``Idempotency-Key`` (scoped per op) or a fresh id."""
    key = str(request.headers.get("idempotency-key") or "").strip()
    if not key:
        return f"{prefix}-{uuid.uuid4().hex}"
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}"


async def _read_json(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body is too large")
        raw.extend(chunk)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _decode_b64(value: Any, *, field: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid base64 in {field}") from exc


def _skill_name(raw: str) -> str:
    try:
        return validate_skill_name(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _version_payload(name: str, head: dict[str, Any], versions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "skill_id": str(head.get("skill_id") or ""),
        "current_version": int(head.get("version") or 0),
        "versions": [int(v["version"]) for v in versions if not v["deleted"]],
        "history": [
            {
                "version": int(v["version"]),
                "action": str(v["action"]),
                "timestamp": _iso(v["created_at"]),
                "tree_sha256": str(v["tree_sha256"]),
                "actor": str(v["actor"]),
                **({"restored_from": int(v["restored_from"])} if v["restored_from"] is not None else {}),
            }
            for v in versions
        ],
    }


def _skill_md_body(raw_md: str) -> str:
    split = frontmatter.split_skill_md(raw_md)
    return split[1] if split is not None else raw_md


def _commit_response(commit, cache: dict[str, Any], *, files: list[str] | None = None) -> dict[str, Any]:
    payload = {**commit.to_response(), "runtime_cache": cache}
    if files is not None:
        payload["files"] = files
        payload["file_count"] = len(files)
    return payload


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def _register_library_reads(router: APIRouter, ctx: _SkillsContext) -> None:
    @router.get("/api/skills")
    async def list_skills():  # noqa: ANN202
        tenant_id = current_tenant_id()
        async with ctx.tx.tenant_tx(tenant_id) as conn:
            heads = await ctx.library.list_skills(conn)
        skills = [
            {
                "name": head["name"],
                "skill_id": head["skill_id"],
                "version": head["version"],
                "description": head["description"],
                "category": head["category"],
                "sha256": head["sha256"],
                "tree_sha256": head["tree_sha256"],
                "files": head["files"],
                "file_count": len(head["files"]),
                "runtime_policy": head["runtime_policy"],
                "updated_by": head["updated_by"],
                "updated_at": _iso(head["updated_at"]),
            }
            for head in heads
        ]
        return {"skill_backend": "postgres", "delivery_mode": "pull", "skills": skills}

    @router.get("/api/skills/{name}")
    async def get_skill(name: str):  # noqa: ANN202
        name = _skill_name(name)
        tenant_id = current_tenant_id()
        async with ctx.tx.tenant_tx(tenant_id) as conn:
            try:
                head, bundle = await ctx.library.read_current_bundle(conn, name)
            except SkillNotFound as exc:
                raise HTTPException(status_code=404, detail=f"Skill not found: {name}") from exc
        raw_md = bundle["SKILL.md"].decode("utf-8", errors="replace")
        body = _skill_md_body(raw_md)
        files = sorted(bundle)
        return {
            "name": name,
            "version": head["version"],
            "description": head["description"],
            "category": head["category"],
            "body": body,
            "skill_md": raw_md,
            "files": files,
            "file_count": len(files),
            "updated_at": _iso(head["updated_at"]),
        }

    @router.get("/api/skills/{name}/versions")
    async def list_versions(name: str):  # noqa: ANN202
        name = _skill_name(name)
        tenant_id = current_tenant_id()
        async with ctx.tx.tenant_tx(tenant_id) as conn:
            head = await ctx.library.get_skill(conn, name, include_deleted=True)
            if head is None:
                raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
            versions = await ctx.library.list_versions(conn, name)
        return _version_payload(name, head, versions)

    @router.get("/api/skills/{name}/versions/{version}")
    async def get_version(name: str, version: int):  # noqa: ANN202
        name = _skill_name(name)
        tenant_id = current_tenant_id()
        async with ctx.tx.tenant_tx(tenant_id) as conn:
            head = await ctx.library.get_skill(conn, name, include_deleted=True)
            if head is None:
                raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
            versions = await ctx.library.list_versions(conn, name)
            try:
                bundle = await ctx.library.read_version_bundle(conn, name, int(version))
            except SkillNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            meta = await ctx.library.get_version(conn, name, int(version)) or {}
            evolution = await version_context(conn, ctx.tx, name, int(version))
        raw_md = bundle["SKILL.md"].decode("utf-8", errors="replace")
        body = _skill_md_body(raw_md)
        info = _version_payload(name, head, versions)
        return {
            "name": name,
            "skill_id": info["skill_id"],
            "version": int(version),
            "current_version": info["current_version"],
            "is_current": int(version) == info["current_version"] and not head["deleted"],
            "versions": info["versions"],
            "description": str(meta.get("description") or ""),
            "category": str(meta.get("category") or "general"),
            "content": body,
            "raw_md": raw_md,
            "tree_sha256": bundle_tree_sha256(bundle),
            "files": [
                {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for path, data in sorted(bundle.items())
            ],
            "action": str(meta.get("action") or ""),
            "created_at": _iso(meta.get("created_at")),
            "evolution": evolution,
        }


# --------------------------------------------------------------------------- #
# writes (tenant admin; one PG transaction each; cache refreshed afterwards)
# --------------------------------------------------------------------------- #


def _register_library_writes(router: APIRouter, ctx: _SkillsContext) -> None:
    @router.post("/api/skills")
    async def save_skill(request: Request):  # noqa: ANN202
        """Create or overwrite a skill's SKILL.md from fields (or raw ``skill_md``).

        Existing bundle files are kept; ``base_version`` (optional) guards the
        read-modify-write against a concurrent edit.
        """
        _require_tenant_admin(request)
        body = await _read_json(request)
        name = _skill_name(str(body.get("name") or ""))
        mutation_id = _mutation_id(request, "console-save")
        tenant_id = current_tenant_id()
        base_version = body.get("base_version")
        try:
            async with ctx.tx.tenant_tx(tenant_id) as conn:
                limits = await ctx.limits(conn)
                try:
                    _head, bundle = await ctx.library.read_current_bundle(conn, name)
                except SkillNotFound:
                    bundle = {}
                created = not bundle
                bundle = dict(bundle)
                bundle["SKILL.md"] = _render_skill_md(name, body, bundle.get("SKILL.md"))
                commit = await ctx.library.commit_bundle(
                    conn, tenant_id=tenant_id, name=name, bundle=bundle, mutation_id=mutation_id,
                    limits=limits, actor=_actor(request), source="console",
                    base_version=int(base_version) if base_version is not None else None,
                )
        except SkillLibraryError as exc:
            raise _http_error(exc) from exc
        cache = ctx.refresh_cache(tenant_id, name, bundle)
        return {**_commit_response(commit, cache, files=sorted(bundle)), "created": created}

    @router.post("/api/skills/{name}/files")
    async def add_files(name: str, request: Request):  # noqa: ANN202
        """Add / replace bundle files: ``{files: [{path, content_b64},...]}``."""
        _require_tenant_admin(request)
        name = _skill_name(name)
        body = await _read_json(request)
        entries = body.get("files")
        if not isinstance(entries, list) or not entries:
            raise HTTPException(status_code=400, detail="files must be a non-empty list")
        incoming: dict[str, bytes] = {}
        for item in entries:
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                raise HTTPException(status_code=400, detail="each file needs a path")
            try:
                rel_path = normalize_bundle_rel_path(str(item["path"]))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if rel_path == "SKILL.md":
                raise HTTPException(status_code=400, detail="Use the editor to change SKILL.md, not file upload")
            incoming[rel_path] = _decode_b64(item.get("content_b64"), field=rel_path)
        mutation_id = _mutation_id(request, "console-files")
        tenant_id = current_tenant_id()
        try:
            async with ctx.tx.tenant_tx(tenant_id) as conn:
                limits = await ctx.limits(conn)
                _head, bundle = await ctx.library.read_current_bundle(conn, name)
                bundle = {**bundle, **incoming}
                commit = await ctx.library.commit_bundle(
                    conn, tenant_id=tenant_id, name=name, bundle=bundle, mutation_id=mutation_id,
                    limits=limits, actor=_actor(request), source="console",
                )
        except SkillLibraryError as exc:
            raise _http_error(exc) from exc
        cache = ctx.refresh_cache(tenant_id, name, bundle)
        return {**_commit_response(commit, cache, files=sorted(bundle)), "written": sorted(incoming)}

    @router.delete("/api/skills/{name}/files/{rel_path:path}")
    async def delete_file(name: str, rel_path: str, request: Request):  # noqa: ANN202
        _require_tenant_admin(request)
        name = _skill_name(name)
        try:
            clean_path = normalize_bundle_rel_path(rel_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if clean_path == "SKILL.md":
            raise HTTPException(status_code=400, detail="SKILL.md cannot be deleted; delete the whole skill instead")
        mutation_id = _mutation_id(request, "console-files")
        tenant_id = current_tenant_id()
        try:
            async with ctx.tx.tenant_tx(tenant_id) as conn:
                limits = await ctx.limits(conn)
                _head, bundle = await ctx.library.read_current_bundle(conn, name)
                if clean_path not in bundle:
                    raise HTTPException(status_code=404, detail=f"{name}/{clean_path} not found")
                bundle = {k: v for k, v in bundle.items() if k != clean_path}
                commit = await ctx.library.commit_bundle(
                    conn, tenant_id=tenant_id, name=name, bundle=bundle, mutation_id=mutation_id,
                    limits=limits, actor=_actor(request), source="console",
                )
        except SkillLibraryError as exc:
            raise _http_error(exc) from exc
        cache = ctx.refresh_cache(tenant_id, name, bundle)
        return {**_commit_response(commit, cache, files=sorted(bundle)), "removed": clean_path}

    @router.post("/api/skills/{name}/rollback")
    async def rollback(name: str, target_version: int, request: Request):  # noqa: ANN202
        """Republish ``target_version``'s content as a new current version."""
        _require_tenant_admin(request)
        name = _skill_name(name)
        mutation_id = _mutation_id(request, f"console-rollback-v{int(target_version)}")
        tenant_id = current_tenant_id()
        try:
            async with ctx.tx.tenant_tx(tenant_id) as conn:
                commit = await ctx.library.commit_rollback(
                    conn, tenant_id=tenant_id, name=name, target_version=int(target_version),
                    mutation_id=mutation_id, actor=_actor(request), source="console",
                )
                bundle = await ctx.library.read_version_bundle(conn, name, commit.version)
        except SkillLibraryError as exc:
            raise _http_error(exc) from exc
        cache = ctx.refresh_cache(tenant_id, name, bundle)
        return {
            **_commit_response(commit, cache),
            "restored_from": int(target_version),
            "new_version": commit.version,
        }

    @router.delete("/api/skills/{name}")
    async def delete_skill(name: str, request: Request):  # noqa: ANN202
        _require_tenant_admin(request)
        name = _skill_name(name)
        mutation_id = _mutation_id(request, "console-delete")
        tenant_id = current_tenant_id()
        try:
            async with ctx.tx.tenant_tx(tenant_id) as conn:
                commit = await ctx.library.commit_delete(
                    conn, tenant_id=tenant_id, name=name, mutation_id=mutation_id,
                    actor=_actor(request), source="console",
                )
        except SkillLibraryError as exc:
            raise _http_error(exc) from exc
        cache = ctx.refresh_cache(tenant_id, name, None)
        return {**_commit_response(commit, cache), "deleted": True}


def _render_skill_md(name: str, body: dict[str, Any], current: bytes | None) -> bytes:
    """Build the new SKILL.md from raw ``skill_md`` or the structured fields.

    Structured edits keep the current document's extra frontmatter keys so a
    round-trip through the editor never drops metadata.
    """
    raw = str(body.get("skill_md") or "")
    if raw.strip():
        return (raw if raw.endswith("\n") else raw + "\n").encode("utf-8")
    description = str(body.get("description") or "")
    if not description.strip():
        raise HTTPException(status_code=400, detail="Skill description must not be empty")
    extra: dict[str, Any] = {}
    if current:
        parsed = frontmatter._load_frontmatter_from_raw(current.decode("utf-8", errors="replace")) or {}
        extra = {k: v for k, v in parsed.items() if k not in {"name", "description", "category"}}
    try:
        rendered = frontmatter.render_skill_md(
            name=name,
            description=description,
            category=str(body.get("category") or "general"),
            content=str(body.get("body") or ""),
            extra_frontmatter=extra,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rendered.encode("utf-8")


# --------------------------------------------------------------------------- #
# Agent pull (tenant machine token + user_id; pull-only delivery)
# --------------------------------------------------------------------------- #


def _register_agent_pull(router: APIRouter, ctx: _SkillsContext) -> None:
    @router.get("/sync/skills")
    async def sync_skills(request: Request, user_id: str = ""):  # noqa: ANN202
        from teamEvolver.security.agent_principal import resolve_agent_principal

        principal = resolve_agent_principal(request, ctx.config, user_id)
        async with ctx.tx.tenant_tx(principal.tenant_id) as conn:
            heads = await ctx.library.list_skills(conn)
            bundles = await ctx.library.read_current_bundles(conn)
        skills = []
        for head in heads:
            bundle = bundles.get(head["name"]) or {}
            skills.append({
                "name": head["name"],
                "skill_id": head["skill_id"],
                "version": head["version"],
                "description": head["description"],
                "category": head["category"],
                "runtime_policy": head["runtime_policy"],
                "sha256": head["sha256"],
                "tree_sha256": head["tree_sha256"],
                "files": [
                    {"path": path, "content_b64": base64.b64encode(data).decode("ascii")}
                    for path, data in sorted(bundle.items())
                ],
            })
        payload = {"status": "ok", "source": "registry", "skills": skills, "total": len(skills)}
        canonical = json.dumps(
            [principal.tenant_id, principal.account_id, [(s["name"], s["version"], s["tree_sha256"]) for s in skills]],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(canonical).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
        validators = {v.strip().removeprefix("W/") for v in request.headers.get("if-none-match", "").split(",")}
        if etag in validators or "*" in validators:
            return Response(status_code=304, headers=headers)
        return JSONResponse(content=payload, headers=headers)


__all__ = ["create_router"]
