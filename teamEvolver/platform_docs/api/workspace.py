"""Platform workspace API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ...admin.openviking_accounts import _openviking_root_key
from ...admin.users import _request_user
from ..workspace import WorkspaceOperations
from ..workspace_core import (
    _MAX_BATCH_CHANGES,
    _MAX_BATCH_CONTENT_BYTES,
    _MAX_CONTENT_BYTES,
    _MAX_TREE_NODES,
    _content_hash,
    _content_text,
    _expand_cli_workspace_args,
    _LocalWorkspaceStore,
    _normalize_cli_argv,
    _normalize_entries,
    _OpenVikingRequestError,
    _OpenVikingUnavailable,
    _scope_map,
    _scope_regular_cli_search,
    _validate_uri,
)


def create_router(config):
    app = APIRouter(tags=["openviking-workspace"])
    owner = WorkspaceOperations()
    owner.config = config


    @app.get("/api/openviking/workspace/config")
    async def workspace_config(request: Request, user_id: str = Query(default="")):
        user, is_admin = owner._workspace_actor(request, user_id)
        deployment = str(
            getattr(owner._workspace_config(), "sharing_viking_deployment", "") or "cloud"
        ).lower()
        endpoint = owner._workspace_endpoint()
        config = owner._workspace_config()
        personal_space = (
            user.get("personal_space")
            if isinstance(user.get("personal_space"), dict)
            else {}
        )
        root_key_configured = bool(_openviking_root_key(config))
        scopes = _scope_map(
            config,
            str(user.get("id") or ""),
            is_admin=is_admin,
            personal_user=str(personal_space.get("viking_user") or ""),
        )
        try:
            owner._workspace_cli_binary()
            cli_available = True
        except HTTPException:
            cli_available = False
        return JSONResponse(content={
            "enabled": bool(
                endpoint
                and root_key_configured
            ),
            "deployment": deployment,
            "endpoint": endpoint,
            "studio_url": f"{endpoint}/studio/" if deployment == "local" and endpoint else "",
            "studio_user_url": (
                f"{endpoint}/studio/home?account="
                f"{str(getattr(config, 'sharing_viking_account', '') or 'default')}"
                f"&user={str(personal_space.get('viking_user') or user.get('id') or '')}"
                if deployment == "local" and endpoint and user.get("id")
                else ""
            ),
            "cli_available": cli_available,
            "cli_full_access": is_admin and bool(_request_user(request).get("platform_role") == "platform_admin"),
            "user_id": str(user.get("id") or ""),
            "account": str(getattr(config, "sharing_viking_account", "") or "default"),
            "root_key_configured": root_key_configured,
            "scopes": {
                name: {
                    "name": scope.name,
                    "root_uri": scope.root_uri,
                    "space": scope.space,
                    "kind": scope.kind,
                    "can_write": scope.can_write,
                    "openviking_user": scope.openviking_user,
                }
                for name, scope in scopes.items()
            },
        })

    @app.get("/api/openviking/workspace/list")
    async def workspace_list(
        request: Request,
        scope: str = Query(...),
        user_id: str = Query(default=""),
        uri: str = Query(default=""),
    ):
        user, selected = owner._workspace_scope(request, user_id, scope)
        target = _validate_uri(selected, uri)
        try:
            result = await owner._workspace_request(
                user,
                selected,
                "GET",
                "/api/v1/fs/ls",
                params={
                    "uri": target,
                    "output": "original",
                    "show_all_hidden": "true",
                    "node_limit": 2000,
                    "sort_by": "name",
                    "sort_order": "asc",
                },
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            local = owner._local_workspace_store(selected)
            entries = _normalize_entries(local.entries(target, recursive=False))
            return JSONResponse(content={
                "scope": selected.name, "root_uri": selected.root_uri,
                "uri": target, "entries": entries,
                "exists": local.exists(target), "can_write": selected.can_write,
                "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc):
                return JSONResponse(content={
                    "scope": selected.name, "root_uri": selected.root_uri,
                    "uri": target, "entries": [], "exists": False,
                    "can_write": selected.can_write,
                })
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return JSONResponse(content={
            "scope": selected.name, "root_uri": selected.root_uri,
            "uri": target, "entries": _normalize_entries(result),
            "exists": True, "can_write": selected.can_write,
        })

    @app.get("/api/openviking/workspace/tree")
    async def workspace_tree(
        request: Request,
        scope: str = Query(...),
        user_id: str = Query(default=""),
        uri: str = Query(default=""),
    ):
        user, selected = owner._workspace_scope(request, user_id, scope)
        target = _validate_uri(selected, uri)
        try:
            result = await owner._workspace_request(
                user,
                selected,
                "GET",
                "/api/v1/fs/tree",
                params={
                    "uri": target,
                    "output": "original",
                    "show_all_hidden": "true",
                    "level_limit": 24,
                    "node_limit": _MAX_TREE_NODES,
                    "sort_by": "name",
                    "sort_order": "asc",
                },
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            local = owner._local_workspace_store(selected)
            entries = [
                entry
                for entry in _normalize_entries(local.entries(target, recursive=True))
                if entry["uri"] != target
            ]
            return JSONResponse(content={
                "scope": selected.name,
                "root_uri": selected.root_uri,
                "uri": target,
                "entries": entries,
                "exists": local.exists(target),
                "can_write": selected.can_write,
                "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc):
                return JSONResponse(content={
                    "scope": selected.name,
                    "root_uri": selected.root_uri,
                    "uri": target,
                    "entries": [],
                    "exists": False,
                    "can_write": selected.can_write,
                })
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        entries = [
            entry
            for entry in _normalize_entries(result)
            if entry["uri"] != target
        ]
        return JSONResponse(content={
            "scope": selected.name,
            "root_uri": selected.root_uri,
            "uri": target,
            "entries": entries,
            "exists": True,
            "can_write": selected.can_write,
        })

    @app.get("/api/openviking/workspace/content")
    async def workspace_content(
        request: Request,
        scope: str = Query(...),
        uri: str = Query(...),
        user_id: str = Query(default=""),
    ):
        user, selected = owner._workspace_scope(request, user_id, scope)
        target = _validate_uri(selected, uri, allow_root=False)
        try:
            result = await owner._workspace_request(
                user, selected, "GET", "/api/v1/content/read",
                params={"uri": target, "offset": 0, "limit": -1, "raw": "true"},
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            local = owner._local_workspace_store(selected)
            try:
                content = local.read(target)
            except _OpenVikingRequestError as read_exc:
                raise HTTPException(status_code=read_exc.status_code, detail=str(read_exc)) from read_exc
            return JSONResponse(content={
                "scope": selected.name, "uri": target,
                "content": content, "can_write": selected.can_write,
                "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return JSONResponse(content={
            "scope": selected.name, "uri": target,
            "content": _content_text(result), "can_write": selected.can_write,
        })

    @app.get("/api/openviking/workspace/level")
    async def workspace_level(
        request: Request,
        scope: str = Query(...),
        uri: str = Query(...),
        level: str = Query(...),
        user_id: str = Query(default=""),
    ):
        user, selected = owner._workspace_scope(request, user_id, scope)
        target = _validate_uri(selected, uri)
        normalized_level = str(level or "").strip().lower()
        if normalized_level not in {"l0", "l1", "abstract", "overview"}:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported OpenViking content level: {level}",
            )
        path = (
            "/api/v1/content/abstract"
            if normalized_level in {"l0", "abstract"}
            else "/api/v1/content/overview"
        )
        try:
            result = await owner._workspace_request(
                user,
                selected,
                "GET",
                path,
                params={"uri": target},
            )
        except _OpenVikingUnavailable:
            # L0/L1 summaries are generated by OpenViking and have no local
            # equivalent; during an outage there is simply nothing to show.
            result = ""
        except _OpenVikingRequestError as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc):
                result = ""
            else:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=str(exc),
                ) from exc
        if isinstance(result, dict):
            content = result.get("content") or result.get("text") or ""
        else:
            content = result or ""
        return JSONResponse(content={
            "scope": selected.name,
            "uri": target,
            "level": "l0" if path.endswith("/abstract") else "l1",
            "content": str(content),
        })

    @app.post("/api/openviking/workspace/content")
    async def workspace_write(request: Request, body: dict[str, Any]):
        user, selected = owner._workspace_scope(
            request, str(body.get("user_id") or ""), str(body.get("scope") or "")
        )
        owner._require_scope_write(selected)
        target = _validate_uri(selected, body.get("uri"), allow_root=False)
        content = str(body.get("content") or "")
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise HTTPException(status_code=413, detail="workspace files cannot exceed 2 MB")
        mode = str(body.get("mode") or "replace").lower()
        if mode not in {"replace", "create", "append"}:
            raise HTTPException(status_code=400, detail=f"unsupported write mode: {mode}")
        try:
            result = await owner._workspace_request(
                user, selected, "POST", "/api/v1/content/write",
                json={"uri": target, "content": content, "mode": mode, "wait": False},
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            local = owner._local_workspace_store(selected)
            try:
                local.write(target, content, mode)
            except _OpenVikingRequestError as write_exc:
                raise HTTPException(status_code=write_exc.status_code, detail=str(write_exc)) from write_exc
            return JSONResponse(content={
                "saved": True, "scope": selected.name, "uri": target,
                "result": None, "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return JSONResponse(content={
            "saved": True, "scope": selected.name, "uri": target, "result": result,
        })

    @app.post("/api/openviking/workspace/batch-content")
    async def workspace_batch_write(request: Request, body: dict[str, Any]):
        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            raise HTTPException(status_code=400, detail="changes must be a non-empty list")
        if len(changes) > _MAX_BATCH_CHANGES:
            raise HTTPException(
                status_code=413,
                detail=f"a workspace edit can contain at most {_MAX_BATCH_CHANGES} files",
            )

        requested_user_id = str(body.get("user_id") or "")
        prepared: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        total_bytes = 0
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"change {index + 1} must be an object",
                )
            user, selected = owner._workspace_scope(
                request,
                requested_user_id,
                str(change.get("scope") or ""),
            )
            owner._require_scope_write(selected)
            if selected.kind not in {"memory", "skills"}:
                raise HTTPException(
                    status_code=400,
                    detail="batch editing is limited to Memory and Skill files",
                )
            target = _validate_uri(selected, change.get("uri"), allow_root=False)
            key = (selected.name, target)
            if key in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"duplicate workspace change: {target}",
                )
            seen.add(key)
            original = change.get("original_content")
            content = change.get("content")
            if not isinstance(original, str) or not isinstance(content, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"workspace change content must be text: {target}",
                )
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > _MAX_CONTENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"workspace files cannot exceed 2 MB: {target}",
                )
            total_bytes += content_bytes
            prepared.append({
                "user": user,
                "scope": selected,
                "uri": target,
                "original": original,
                "content": content,
            })
        if total_bytes > _MAX_BATCH_CONTENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="workspace edit content cannot exceed 16 MB in total",
            )

        # Pre-check phase. Reading each file both detects concurrent edits
        # (optimistic lock) and probes OpenViking availability. If OV is
        # unreachable here — before any write has landed — the entire batch
        # is safely redirected to local/NAS storage.
        ov_unavailable = False
        conflicts: list[str] = []
        for change in prepared:
            try:
                current = await owner._workspace_request(
                    change["user"],
                    change["scope"],
                    "GET",
                    "/api/v1/content/read",
                    params={
                        "uri": change["uri"],
                        "offset": 0,
                        "limit": -1,
                        "raw": "true",
                    },
                )
            except _OpenVikingUnavailable as exc:
                if not owner._workspace_fallback_enabled():
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                ov_unavailable = True
                break
            except _OpenVikingRequestError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            current_content = _content_text(current)
            # Retrying after a lost response is safe when the desired bytes
            # already landed, even though the original base no longer matches.
            if current_content not in {change["original"], change["content"]}:
                conflicts.append(change["uri"])

        if ov_unavailable:
            # OpenViking is down and nothing has been written yet: commit the
            # whole batch to the local/NAS tree the Skill backend shares,
            # re-running the conflict check against local content.
            local_conflicts: list[str] = []
            stores: list[tuple[dict[str, Any], "_LocalWorkspaceStore"]] = []
            for change in prepared:
                local = owner._local_workspace_store(change["scope"])
                try:
                    current = local.read(change["uri"])
                except _OpenVikingRequestError:
                    current = ""  # not yet on NAS — a fresh create
                if current not in {change["original"], change["content"]}:
                    local_conflicts.append(change["uri"])
                stores.append((change, local))
            if local_conflicts:
                shown = ", ".join(local_conflicts[:5])
                suffix = f" and {len(local_conflicts) - 5} more" if len(local_conflicts) > 5 else ""
                raise HTTPException(
                    status_code=409,
                    detail=f"workspace files changed since editing began: {shown}{suffix}",
                )
            for change, local in stores:
                local.write(change["uri"], change["content"], "replace")
            return JSONResponse(content={
                "saved": True,
                "changed_count": len(prepared),
                "results": [],
                "fallback": "local",
            })

        if conflicts:
            shown = ", ".join(conflicts[:5])
            suffix = f" and {len(conflicts) - 5} more" if len(conflicts) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail=f"workspace files changed since editing began: {shown}{suffix}",
            )

        groups: dict[str, dict[str, Any]] = {}
        for change in prepared:
            selected = change["scope"]
            group = groups.setdefault(
                selected.name,
                {
                    "user": change["user"],
                    "scope": selected,
                    "operations": [],
                },
            )
            group["operations"].append({
                "uri": change["uri"],
                "content": change["content"],
                "precondition": {
                    "kind": "replace_if_hash",
                    "base_hash": _content_hash(change["original"]),
                },
            })

        results: list[dict[str, Any]] = []
        for group in groups.values():
            selected = group["scope"]
            try:
                result = await owner._workspace_request(
                    group["user"],
                    selected,
                    "POST",
                    "/api/v1/content/batch-write",
                    json={
                        "root_uri": selected.root_uri,
                        "operations": group["operations"],
                        "wait": False,
                    },
                )
            except _OpenVikingUnavailable:
                # OpenViking answered reads but the batch-write endpoint is
                # unhealthy (it has deadlocked before, see storage/viking.py).
                # Degrade to per-object writes against the *same* live OV so
                # data does not split between OV (reads) and NAS (writes).
                try:
                    for operation in group["operations"]:
                        await owner._workspace_request(
                            group["user"],
                            selected,
                            "POST",
                            "/api/v1/content/write",
                            json={
                                "uri": operation["uri"],
                                "content": operation["content"],
                                "mode": "replace",
                                "wait": False,
                            },
                        )
                except _OpenVikingUnavailable as inner:
                    # OV died mid-flight; earlier groups may already be on OV,
                    # so falling to NAS now would split the edit. Surface 503.
                    raise HTTPException(status_code=503, detail=str(inner)) from inner
                except _OpenVikingRequestError as inner:
                    raise HTTPException(status_code=inner.status_code, detail=str(inner)) from inner
                result = {"mode": "per_object_fallback"}
            except _OpenVikingRequestError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            results.append({
                "scope": selected.name,
                "root_uri": selected.root_uri,
                "result": result,
            })
        return JSONResponse(content={
            "saved": True,
            "changed_count": len(prepared),
            "results": results,
        })

    @app.post("/api/openviking/workspace/mkdir")
    async def workspace_mkdir(request: Request, body: dict[str, Any]):
        user, selected = owner._workspace_scope(
            request, str(body.get("user_id") or ""), str(body.get("scope") or "")
        )
        owner._require_scope_write(selected)
        target = _validate_uri(selected, body.get("uri"), allow_root=False)
        try:
            result = await owner._workspace_request(
                user, selected, "POST", "/api/v1/fs/mkdir",
                json={"uri": target, "description": str(body.get("description") or "") or None},
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            # Object stores are key-oriented: directories are implicit and
            # materialize when a file is written under them. Report success
            # so the console can proceed to create files in this path.
            return JSONResponse(content={
                "created": True, "scope": selected.name, "uri": target,
                "result": None, "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return JSONResponse(content={
            "created": True, "scope": selected.name, "uri": target, "result": result,
        })

    @app.post("/api/openviking/workspace/cli")
    async def workspace_cli(request: Request, body: dict[str, Any]):
        user, selected = owner._workspace_scope(
            request,
            str(body.get("user_id") or ""),
            str(body.get("scope") or ""),
        )
        current_uri = _validate_uri(selected, body.get("current_uri"))
        argv = _expand_cli_workspace_args(
            _normalize_cli_argv(str(body.get("command") or "")),
            current_uri,
        )
        from ...admin.authorization import is_platform_admin, is_tenant_admin
        is_admin = is_tenant_admin(_request_user(request)) and is_platform_admin(_request_user(request))
        if not is_admin:
            argv = _scope_regular_cli_search(argv, current_uri)
        result = await owner._workspace_cli(
            user,
            selected,
            argv,
            is_admin=is_admin,
        )
        return JSONResponse(content=result)

    @app.get("/api/openviking/workspace/aggregate")
    async def workspace_aggregate(
        request: Request,
        user_id: str = Query(default=""),
        kind: str = Query(default="all"),
    ):
        """Union of personal + team entries for a user, tagged by source.

        Returns three grouped lists — skills, memories, resources — each
        entry annotated with ``source`` (``personal`` / ``team``) so the
        console can render a single unified library while retaining the
        provenance the user needs to decide where to edit.
        """
        requested = str(kind or "all").strip().lower()
        valid_kinds = {"skills", "memory", "resources", "all"}
        if requested not in valid_kinds:
            raise HTTPException(status_code=400, detail=f"unsupported kind: {kind}")
        user, is_admin = owner._workspace_actor(request, user_id)
        personal_space = (
            user.get("personal_space")
            if isinstance(user.get("personal_space"), dict)
            else {}
        )
        scopes = _scope_map(
            owner._workspace_config(),
            str(user.get("id") or ""),
            is_admin=is_admin,
            personal_user=str(personal_space.get("viking_user") or ""),
        )

        async def _entries(scope_name: str) -> list[dict[str, Any]]:
            scope = scopes[scope_name]
            try:
                raw = await owner._workspace_request(
                    user,
                    scope,
                    "GET",
                    "/api/v1/fs/ls",
                    params={
                        "uri": scope.root_uri,
                        "output": "original",
                        "show_all_hidden": "false",
                        "node_limit": 500,
                        "sort_by": "name",
                        "sort_order": "asc",
                    },
                )
            except _OpenVikingUnavailable:
                if not owner._workspace_fallback_enabled():
                    return []
                local = owner._local_workspace_store(scope)
                raw = local.entries(scope.root_uri, recursive=False)
            except _OpenVikingRequestError:
                return []
            items = _normalize_entries(raw)
            for entry in items:
                entry["source"] = scope.space
                entry["scope"] = scope.name
                entry["scope_root_uri"] = scope.root_uri
                entry["can_write"] = scope.can_write
            return items

        groups: dict[str, list[dict[str, Any]]] = {}
        if requested in {"skills", "all"}:
            personal = await _entries("personal_skills")
            team = await _entries("team_skills")
            groups["skills"] = personal + team
        if requested in {"memory", "all"}:
            personal = await _entries("personal_memory")
            team = await _entries("team_memory")
            groups["memory"] = personal + team
        if requested in {"resources", "all"}:
            personal = await _entries("personal_resources")
            team = await _entries("team_resources")
            groups["resources"] = personal + team

        summary = {
            key: {
                "total": len(items),
                "personal": sum(1 for i in items if i.get("source") == "personal"),
                "team": sum(1 for i in items if i.get("source") == "team"),
            }
            for key, items in groups.items()
        }
        return JSONResponse(
            content={
                "user_id": str(user.get("id") or ""),
                "is_admin": is_admin,
                "groups": groups,
                "summary": summary,
                "scopes": {
                    name: {
                        "name": scope.name,
                        "root_uri": scope.root_uri,
                        "space": scope.space,
                        "kind": scope.kind,
                        "can_write": scope.can_write,
                        "openviking_user": scope.openviking_user,
                    }
                    for name, scope in scopes.items()
                },
            }
        )

    @app.delete("/api/openviking/workspace")
    async def workspace_delete(
        request: Request,
        scope: str = Query(...),
        uri: str = Query(...),
        user_id: str = Query(default=""),
    ):
        user, selected = owner._workspace_scope(request, user_id, scope)
        owner._require_scope_write(selected)
        target = _validate_uri(selected, uri, allow_root=False)
        try:
            result = await owner._workspace_request(
                user, selected, "DELETE", "/api/v1/fs",
                params={"uri": target, "recursive": "true", "wait": "false"},
            )
        except _OpenVikingUnavailable as exc:
            if not owner._workspace_fallback_enabled():
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            owner._local_workspace_store(selected).delete(target)
            return JSONResponse(content={
                "deleted": True, "scope": selected.name, "uri": target,
                "result": None, "fallback": "local",
            })
        except _OpenVikingRequestError as exc:
            if exc.status_code != 404 and "NOT_FOUND" not in str(exc):
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            result = None
        return JSONResponse(content={
            "deleted": True, "scope": selected.name, "uri": target, "result": result,
        })

    return app
