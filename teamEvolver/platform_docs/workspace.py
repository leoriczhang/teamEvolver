"""OpenViking workspace operations with explicit tenant authorization."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
from starlette.exceptions import HTTPException
from starlette.requests import Request

from ..admin.openviking_accounts import _openviking_root_key
from ..admin.users import _find_user, _request_user, _require_self_or_admin
from .workspace_core import (
    _ANSI_ESCAPE_RE,
    _CLI_TIMEOUT_SECONDS,
    _MAX_CLI_OUTPUT_BYTES,
    _SCOPES,
    IdentityStore,
    _error_message,
    _LocalWorkspaceStore,
    _OpenVikingRequestError,
    _OpenVikingUnavailable,
    _scope_fallback_root,
    _scope_map,
    _validate_regular_cli_scope,
    _WorkspaceScope,
)


class WorkspaceOperations:
    """Workspace, memory, and skill browsing backed by OpenViking APIs."""

    def _workspace_config(self):
        from ..tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), self.config)

    def _workspace_endpoint(self) -> str:
        return str(getattr(self._workspace_config(), "sharing_viking_endpoint", "") or "").strip().rstrip("/")

    def _workspace_fallback_enabled(self) -> bool:
        """True when console workspace ops may fall back to local/NAS storage.

        Mirrors the built-in object-store fallback switch
        (``sharing_local_fallback_enabled``); when off, OpenViking outages
        surface as 503 exactly as before this feature existed.
        """
        config = self._workspace_config()
        return not config.storage_pg_enabled and bool(config.sharing_local_fallback_enabled)

    def _local_workspace_store(self, scope: _WorkspaceScope) -> "_LocalWorkspaceStore":
        """Build the local/NAS view for *scope*, rooted where the Skill backend
        falls back, so console edits during an outage share the backend's tree.
        """
        root, key_base = _scope_fallback_root(
            self._workspace_config(), scope, self._workspace_endpoint()
        )
        return _LocalWorkspaceStore(root, key_base, scope_root=scope.root_uri)

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
        registry = IdentityStore(self.config).load_registry()
        _index, user = _find_user(registry, target_id)
        from ..admin.authorization import is_tenant_admin
        return user, is_tenant_admin(current)

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
            self._workspace_config(),
            str(user.get("id") or ""),
            is_admin=is_admin,
            personal_user=str(personal_space.get("viking_user") or ""),
        )
        return user, scopes[scope_name]

    def _workspace_headers(
        self, _user: dict[str, Any], scope: _WorkspaceScope
    ) -> dict[str, str]:
        config = self._workspace_config()
        api_key = _openviking_root_key(config)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-OpenViking-Account": str(getattr(config, "sharing_viking_account", "") or "default"),
            "X-OpenViking-User": scope.openviking_user or "default",
            "X-OpenViking-Agent": str(getattr(config, "sharing_viking_agent", "") or "team-skill-evolver"),
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
                # Transport error / timeout: the server never answered, so this
                # is an availability failure eligible for the local/NAS
                # fallback (see _OpenVikingUnavailable).
                raise _OpenVikingUnavailable(f"OpenViking is unreachable: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 500:
            # 5xx: the gateway is unhealthy — treat as unavailable so callers
            # can fall back. 4xx below stays a hard error (live server said no).
            raise _OpenVikingUnavailable(_error_message(response, data))
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
