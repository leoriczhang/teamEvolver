"""Authenticated OpenViking workspace bridge for the teamEvolver console."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .users_admin import (
    _effective_team_key,
    _find_user,
    _load_registry,
    _registry_path,
    _request_user,
    _require_self_or_admin,
    _space_key,
)

_SCOPES = {
    "personal_memory", "team_memory", "personal_skills", "team_skills",
    "personal_resources", "team_resources",
    "personal_workspace", "team_workspace", "platform_assets",
}
_MAX_CONTENT_BYTES = 2 * 1024 * 1024
_MAX_BATCH_CHANGES = 100
_MAX_BATCH_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_TREE_NODES = 10_000
_MAX_CLI_OUTPUT_BYTES = 512 * 1024
_CLI_TIMEOUT_SECONDS = 300
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_REGULAR_USER_CLI_COMMANDS = {
    "abstract",
    "find",
    "glob",
    "grep",
    "health",
    "ls",
    "overview",
    "read",
    "search",
    "session",
    "stat",
    "status",
    "tree",
    "version",
}


@dataclass(frozen=True)
class _WorkspaceScope:
    name: str
    root_uri: str
    space: str
    kind: str
    can_write: bool
    openviking_user: str


class _OpenVikingRequestError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _scope_map(
    config,
    user_id: str,
    *,
    is_admin: bool,
    personal_user: str = "",
) -> dict[str, _WorkspaceScope]:
    prefix = str(
        getattr(config, "sharing_viking_root_prefix", "") or "team-skill-evolver"
    ).strip().strip("/")
    shared = f"viking://resources/{prefix}"
    personal_owner = str(
        personal_user
        or user_id
        or getattr(config, "sharing_viking_personal_user", "")
    ).strip()
    # Identity used to reach account-shared team resources. It is only the
    # ``X-OpenViking-User`` header value for viking://resources/* access (which
    # is account-shared and role-agnostic), not a private storage owner. Read it
    # from config (``sharing_viking_user``, default ``team``) so deployments can
    # point it at any real account user instead of the hardcoded ``team`` name.
    # trusted mode still requires *some* concrete user header, so this must not
    # be empty; it must never fall back to ``default`` (an OpenViking bootstrap
    # identity rather than a team workspace).
    team_owner = str(getattr(config, "sharing_viking_user", "") or "team").strip() or "team"
    # Team memory now lives in the account-shared knowledge base produced by the
    # aggregation pipeline (viking://resources/<prefix>), not under the team
    # user's private memory namespace. This aligns the workspace view with where
    # aggregated team knowledge is actually written and readable account-wide.
    agg_prefix = str(
        getattr(config, "aggregation_shared_knowledge_prefix", "") or "shared-knowledge"
    ).strip().strip("/")
    team_memory_root = f"viking://resources/{agg_prefix}"
    personal = f"viking://user/{personal_owner}"
    peer = f"{shared}/peers/{user_id}"
    return {
        "personal_memory": _WorkspaceScope(
            "personal_memory",
            f"{personal}/memories",
            "personal",
            "memory",
            True,
            personal_owner,
        ),
        "team_memory": _WorkspaceScope(
            "team_memory",
            team_memory_root,
            "team",
            "memory",
            is_admin,
            team_owner,
        ),
        "personal_skills": _WorkspaceScope(
            "personal_skills",
            f"{peer}/skills",
            "personal",
            "skills",
            True,
            personal_owner,
        ),
        "team_skills": _WorkspaceScope(
            "team_skills",
            f"{shared}/skills",
            "team",
            "skills",
            is_admin,
            team_owner,
        ),
        "personal_resources": _WorkspaceScope(
            "personal_resources",
            f"{personal}/resources",
            "personal",
            "resources",
            True,
            personal_owner,
        ),
        "team_resources": _WorkspaceScope(
            "team_resources",
            "viking://resources/team",
            "team",
            "resources",
            is_admin,
            team_owner,
        ),
        "personal_workspace": _WorkspaceScope(
            "personal_workspace",
            personal,
            "personal",
            "workspace",
            True,
            personal_owner,
        ),
        "team_workspace": _WorkspaceScope(
            "team_workspace",
            shared,
            "team",
            "workspace",
            is_admin,
            team_owner,
        ),
        # Self-evolution platform's own storage under the team resources root
        # (sessions, candidate_skills, validation_*, memory-changes, skill_lab,
        # ...). These are internal artifacts that an Agent cannot reference, so
        # the console exposes them read-only in a separate "平台资产" view.
        "platform_assets": _WorkspaceScope(
            "platform_assets",
            shared,
            "team",
            "platform",
            False,
            team_owner,
        ),
    }


def _validate_uri(scope: _WorkspaceScope, value: Any, *, allow_root: bool = True) -> str:
    root = scope.root_uri.rstrip("/")
    uri = str(value or root).strip().rstrip("/") or root
    if "\\" in uri or any(part == ".." for part in uri.split("/")):
        raise HTTPException(status_code=400, detail="invalid workspace URI")
    if uri != root and not uri.startswith(f"{root}/"):
        raise HTTPException(status_code=403, detail="workspace URI is outside the selected scope")
    if not allow_root and uri == root:
        raise HTTPException(status_code=400, detail="the workspace root cannot be modified")
    return uri


def _normalize_entries(result: Any) -> list[dict[str, Any]]:
    entries = result
    if isinstance(result, dict):
        entries = result.get("entries") or result.get("items") or []
    if not isinstance(entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        uri = str(raw.get("uri") or raw.get("path") or "")
        if not uri:
            continue
        normalized.append({
            "uri": uri.rstrip("/"),
            "name": str(raw.get("name") or uri.rstrip("/").rsplit("/", 1)[-1]),
            "is_dir": bool(raw.get("isDir", raw.get("is_dir", uri.endswith("/")))),
            "size": raw.get("size_bytes", raw.get("size")),
            "modified_at": raw.get("modTime", raw.get("mod_time", raw.get("modified_at", ""))),
            "abstract": str(raw.get("abstract") or ""),
            "relative_path": str(raw.get("rel_path") or raw.get("relative_path") or ""),
        })
    return sorted(normalized, key=lambda item: (not item["is_dir"], item["name"].lower()))


def _normalize_cli_argv(command: str) -> list[str]:
    raw = str(command or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="CLI command is required")
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid CLI command: {exc}") from exc
    if argv and argv[0] in {"ov", "openviking"}:
        argv = argv[1:]
    if not argv:
        raise HTTPException(status_code=400, detail="CLI command is required")
    slash_style = argv[0].startswith("/")
    if slash_style:
        argv[0] = argv[0][1:]
    if slash_style and argv[0] == "session" and len(argv) > 1:
        aliases = {
            "create": "new",
            "context": "get-session-context",
            "archive": "get-session-archive",
            "message": "add-message",
        }
        argv[1] = aliases.get(argv[1], argv[1])
    return argv


def _expand_cli_workspace_args(argv: list[str], current_uri: str) -> list[str]:
    expanded = list(argv)
    command = _cli_command_name(expanded)
    if command in {"ls", "tree"} and len(expanded) == 1:
        expanded.append(current_uri)
    if command in {"find", "search"}:
        for index, token in enumerate(expanded):
            if token == "--scope":
                expanded[index] = "--uri"
                if index + 1 < len(expanded) and expanded[index + 1] == ".":
                    expanded[index + 1] = current_uri
            elif token.startswith("--scope="):
                value = token.split("=", 1)[1]
                expanded[index] = f"--uri={current_uri if value == '.' else value}"
    if command == "wait" and len(expanded) == 2 and not expanded[1].startswith("-"):
        expanded = ["wait", "--timeout", expanded[1]]
    return expanded


def _cli_command_name(argv: list[str]) -> str:
    for token in argv:
        if not token.startswith("-"):
            return token.lower()
    return ""


def _validate_regular_cli_scope(argv: list[str], scope: _WorkspaceScope) -> None:
    if any(
        token in {"--account", "--user", "--sudo"}
        or token.startswith(("--account=", "--user="))
        for token in argv
    ):
        raise HTTPException(
            status_code=403,
            detail="CLI identity overrides require an administrator",
        )
    command = _cli_command_name(argv)
    if command not in _REGULAR_USER_CLI_COMMANDS:
        raise HTTPException(
            status_code=403,
            detail="this OpenViking CLI command requires an administrator",
        )
    root = scope.root_uri.rstrip("/")
    for token in argv:
        marker = token.find("viking://")
        if marker < 0:
            continue
        uri = token[marker:].rstrip("/")
        if uri != root and not uri.startswith(f"{root}/"):
            raise HTTPException(
                status_code=403,
                detail="CLI URI is outside the selected workspace scope",
            )


def _scope_regular_cli_search(
    argv: list[str],
    current_uri: str,
) -> list[str]:
    command = _cli_command_name(argv)
    if command not in {"find", "glob", "grep", "search"}:
        return argv
    if any(
        token in {"-u", "--uri"} or token.startswith("--uri=")
        for token in argv
    ):
        return argv
    return [*argv, "--uri", current_uri]


def _error_message(response: httpx.Response, data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return f"{error.get('code') or 'OPENVIKING_ERROR'}: {error.get('message') or ''}".rstrip(": ")
        if data.get("detail"):
            return str(data["detail"])
    return f"OpenViking HTTP {response.status_code}: {response.text[:300]}"


def _content_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("content") or result.get("text") or "")
    return str(result or "")


def _content_hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


class OpenVikingWorkspaceMixin:
    """Workspace, memory, and skill browsing backed by OpenViking APIs."""

    def _workspace_endpoint(self) -> str:
        return str(getattr(self.config, "sharing_viking_endpoint", "") or "").strip().rstrip("/")

    @staticmethod
    def _workspace_cli_binary() -> str:
        configured = str(os.environ.get("OPENVIKING_CLI_BIN") or "").strip()
        candidates = [
            configured,
            str(Path.home() / "OpenViking" / "target" / "release" / "ov"),
            str(Path.home() / "miniconda3" / "envs" / "openviking" / "bin" / "ov"),
            shutil.which("ov") or "",
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise HTTPException(
            status_code=503,
            detail="OpenViking CLI binary is unavailable; set OPENVIKING_CLI_BIN",
        )

    def _workspace_actor(
        self, request: Request, requested_user_id: str
    ) -> tuple[dict[str, Any], bool]:
        current = _request_user(request)
        target_id = str(requested_user_id or current.get("id") or "").strip()
        if not target_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        _require_self_or_admin(request, target_id)
        registry = _load_registry(_registry_path(self.config))
        _index, user = _find_user(registry, target_id)
        return user, str(current.get("role") or "user") == "admin"

    def _workspace_scope(
        self, request: Request, requested_user_id: str, scope_name: str
    ) -> tuple[dict[str, Any], _WorkspaceScope]:
        if scope_name not in _SCOPES:
            raise HTTPException(status_code=400, detail=f"unsupported workspace scope: {scope_name}")
        user, is_admin = self._workspace_actor(request, requested_user_id)
        personal_space = (
            user.get("personal_space")
            if isinstance(user.get("personal_space"), dict)
            else {}
        )
        scopes = _scope_map(
            self.config,
            str(user.get("id") or ""),
            is_admin=is_admin,
            personal_user=str(personal_space.get("viking_user") or ""),
        )
        return user, scopes[scope_name]

    def _workspace_headers(
        self, user: dict[str, Any], scope: _WorkspaceScope
    ) -> dict[str, str]:
        if scope.space == "team":
            api_key = _space_key(user.get("team_space") or {}) or _effective_team_key(self.config)
        else:
            api_key = _space_key(user.get("personal_space") or {}) or str(
                getattr(self.config, "sharing_viking_personal_api_key", "") or ""
            )
            # A deployment commonly has one OpenViking service credential for
            # the team. Personal workspaces remain scoped by their
            # OpenViking user and URI headers, so fall back to that credential
            # only when a personal credential has not been configured.
            api_key = api_key or _space_key(user.get("team_space") or {}) or _effective_team_key(
                self.config
            )
        api_key = api_key or str(getattr(self.config, "sharing_viking_api_key", "") or "")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-OpenViking-Account": str(getattr(self.config, "sharing_viking_account", "") or "default"),
            "X-OpenViking-User": scope.openviking_user or "default",
            "X-OpenViking-Agent": str(getattr(self.config, "sharing_viking_agent", "") or "team-skill-evolver"),
        }
        if api_key:
            headers["X-API-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _workspace_request(
        self,
        user: dict[str, Any],
        scope: _WorkspaceScope,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        endpoint = self._workspace_endpoint()
        if not endpoint:
            raise HTTPException(status_code=503, detail="OpenViking endpoint is not configured")
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.request(
                    method,
                    f"{endpoint}{path}",
                    headers=self._workspace_headers(user, scope),
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=503, detail=f"OpenViking is unreachable: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            raise _OpenVikingRequestError(response.status_code, _error_message(response, data))
        if isinstance(data, dict) and data.get("status") == "error":
            raise _OpenVikingRequestError(502, _error_message(response, data))
        return data.get("result") if isinstance(data, dict) and "result" in data else data

    async def _workspace_cli(
        self,
        user: dict[str, Any],
        scope: _WorkspaceScope,
        argv: list[str],
        *,
        is_admin: bool,
    ) -> dict[str, Any]:
        if not is_admin:
            _validate_regular_cli_scope(argv, scope)
        binary = self._workspace_cli_binary()
        headers = self._workspace_headers(user, scope)
        api_key = str(headers.get("X-API-Key") or "")
        config = {
            "url": self._workspace_endpoint(),
            "api_key": api_key or None,
            "root_api_key": api_key if is_admin else None,
            "account": headers["X-OpenViking-Account"],
            "user": headers["X-OpenViking-User"],
            "agent_id": headers["X-OpenViking-Agent"],
            "timeout": float(_CLI_TIMEOUT_SECONDS),
            "output": "json",
            "echo_command": False,
            "show_progress": False,
        }
        with tempfile.TemporaryDirectory(prefix="teamEvolver-ovcli-") as temp_dir:
            config_path = os.path.join(temp_dir, "ovcli.conf")
            settings_path = os.path.join(temp_dir, ".openviking", "ovcli.settings.conf")
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=True)
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({"language": "zh-CN"}, handle, ensure_ascii=True)
            os.chmod(config_path, 0o600)
            os.chmod(settings_path, 0o600)
            env = os.environ.copy()
            env["HOME"] = temp_dir
            env["OPENVIKING_CLI_CONFIG_FILE"] = config_path
            env["OPENVIKING_LANG"] = "zh-CN"
            env["NO_COLOR"] = "1"
            process = await asyncio.create_subprocess_exec(
                binary,
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=_CLI_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise HTTPException(
                    status_code=504,
                    detail=f"OpenViking CLI timed out after {_CLI_TIMEOUT_SECONDS}s",
                )
        output = stdout[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
        error = stderr[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
        output = _ANSI_ESCAPE_RE.sub("", output).strip()
        error = _ANSI_ESCAPE_RE.sub("", error).strip()
        return {
            "ok": process.returncode == 0,
            "exit_code": process.returncode,
            "command": ["ov", *argv],
            "stdout": output,
            "stderr": error,
            "truncated": (
                len(stdout) > _MAX_CLI_OUTPUT_BYTES
                or len(stderr) > _MAX_CLI_OUTPUT_BYTES
            ),
        }

    @staticmethod
    def _require_scope_write(scope: _WorkspaceScope) -> None:
        if not scope.can_write:
            raise HTTPException(
                status_code=403,
                detail="only administrators can modify team OpenViking scopes",
            )

    def _register_openviking_workspace_routes(self, app: FastAPI) -> None:
        owner = self

        @app.get("/api/openviking/workspace/config")
        async def workspace_config(request: Request, user_id: str = Query(default="")):
            user, is_admin = owner._workspace_actor(request, user_id)
            deployment = str(
                getattr(owner.config, "sharing_viking_deployment", "") or "cloud"
            ).lower()
            endpoint = owner._workspace_endpoint()
            personal_space = (
                user.get("personal_space")
                if isinstance(user.get("personal_space"), dict)
                else {}
            )
            # A dedicated personal OpenViking key always grants personal access.
            # In a local (Trusted) deployment the server credential plus the
            # X-OpenViking-User header is sufficient to reach
            # viking://user/<person>/..., so no per-user credential is needed —
            # auto-grant personal access whenever a usable server key exists.
            has_personal_key = bool(
                _space_key(personal_space)
                or str(getattr(owner.config, "sharing_viking_personal_api_key", "") or "")
            )
            has_server_key = bool(
                _effective_team_key(owner.config)
                or str(getattr(owner.config, "sharing_viking_api_key", "") or "")
            )
            personal_access_configured = has_personal_key or (
                deployment == "local" and has_server_key
            )
            scopes = _scope_map(
                owner.config,
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
                "enabled": bool(getattr(owner.config, "sharing_enabled", False) and endpoint),
                "deployment": deployment,
                "endpoint": endpoint,
                "studio_url": f"{endpoint}/studio/" if deployment == "local" and endpoint else "",
                "studio_user_url": (
                    f"{endpoint}/studio/home?account="
                    f"{str(getattr(owner.config, 'sharing_viking_account', '') or 'default')}"
                    f"&user={str(user.get('id') or '')}"
                    if deployment == "local" and endpoint and user.get("id")
                    else ""
                ),
                "cli_available": cli_available,
                "cli_full_access": is_admin,
                "user_id": str(user.get("id") or ""),
                # A team credential intentionally does not imply permission to
                # read viking://user/<person>/... . The console uses this flag
                # to select the usable team scope until a personal credential
                # has been supplied in user administration.
                "personal_access_configured": personal_access_configured,
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
                except _OpenVikingRequestError as exc:
                    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
                current_content = _content_text(current)
                # Retrying after a lost response is safe when the desired bytes
                # already landed, even though the original base no longer matches.
                if current_content not in {change["original"], change["content"]}:
                    conflicts.append(change["uri"])
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
            is_admin = str(_request_user(request).get("role") or "user") == "admin"
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
                owner.config,
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
            except _OpenVikingRequestError as exc:
                if exc.status_code != 404 and "NOT_FOUND" not in str(exc):
                    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
                result = None
            return JSONResponse(content={
                "deleted": True, "scope": selected.name, "uri": target, "result": result,
            })
