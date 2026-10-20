"""OpenViking URI, scope and CLI helpers."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.exceptions import HTTPException

from ..admin.identity_store import IdentityStore
from ..admin.openviking_accounts import _openviking_root_key
from ..admin.users import _find_user, _request_user, _require_self_or_admin
from ..storage import _VIKING_ROOT_PREFIX, LocalObjectStore, _effective_fallback_root, is_not_found_error

__all__ = [
    "_SCOPES",
    "_MAX_CONTENT_BYTES",
    "_MAX_BATCH_CHANGES",
    "_MAX_BATCH_CONTENT_BYTES",
    "_MAX_TREE_NODES",
    "_MAX_CLI_OUTPUT_BYTES",
    "_CLI_TIMEOUT_SECONDS",
    "_ANSI_ESCAPE_RE",
    "_REGULAR_USER_CLI_COMMANDS",
    "_WorkspaceScope",
    "_OpenVikingRequestError",
    "_OpenVikingUnavailable",
    "_scope_map",
    "_validate_uri",
    "_normalize_entries",
    "_normalize_cli_argv",
    "_expand_cli_workspace_args",
    "_cli_command_name",
    "_validate_regular_cli_scope",
    "_scope_regular_cli_search",
    "_error_message",
    "_content_text",
    "_content_hash",
    "_parse_viking_uri",
    "_scope_fallback_root",
    "_LocalWorkspaceStore",
    "IdentityStore",
    "_find_user",
    "_openviking_root_key",
    "_request_user",
    "_require_self_or_admin",
]

_SCOPES = {
    "personal_memory", "team_memory", "personal_skills", "team_skills",
    "personal_resources", "team_resources",
    "personal_workspace", "team_workspace",
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


class _OpenVikingUnavailable(RuntimeError):
    """OpenViking is currently unreachable (transport error, timeout, 5xx).

    Distinct from :class:`_OpenVikingRequestError` (a 4xx answer from a live
    server, e.g. NOT_FOUND / auth): only *unavailability* is eligible for the
    built-in local/NAS fallback, matching ``build_object_store``'s policy —
    a 4xx must surface so a misconfiguration is not masked by local writes.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    personal_owner = str(personal_user or user_id).strip()
    # Identity used to reach account-shared team resources. It is only the
    # ``X-OpenViking-User`` header value for viking://resources/* access (which
    # is account-shared and role-agnostic), not a private storage owner. Read it
    # from config (``sharing_viking_user``, default ``team``) so deployments can
    # point it at any real account user instead of the hardcoded ``team`` name.
    # trusted mode still requires *some* concrete user header, so this must not
    # be empty; it must never fall back to ``default`` (an OpenViking bootstrap
    # identity rather than a team workspace).
    team_owner = str(getattr(config, "sharing_viking_user", "") or "team").strip() or "team"
    # Team memory now lives in the account-shared output produced by the
    # aggregation pipeline (viking://resources/<prefix>), not under the team
    # user's private memory namespace. This aligns the workspace view with where
    # aggregated team memory is actually written and readable account-wide.
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
            "viking://user",
            "personal",
            "workspace",
            True,
            personal_owner,
        ),
        "team_workspace": _WorkspaceScope(
            "team_workspace",
            "viking://resources",
            "team",
            "workspace",
            is_admin,
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


def _parse_viking_uri(root_uri: str) -> tuple[str, str, str]:
    """Split a scope root URI into (namespace, root_prefix, key_base).

    ``key_base`` is the OpenViking base prefix that object-store keys are
    relative to — the same split :class:`OpenVikingObjectStore` uses. For
    ``viking://resources/team-skill-evolver/skills`` this yields
    (``resources``, ``team-skill-evolver``, ``viking://resources/team-skill-evolver/``)
    so the file ``.../skills/foo/SKILL.md`` maps to key ``skills/foo/SKILL.md`` —
    identical to what the Skill backend writes, so a console edit during an
    outage lands on the very NAS files the backend already fell back to.
    """
    raw = str(root_uri or "").strip().rstrip("/")
    for scheme_root, namespace in (("viking://resources/", "resources"), ("viking://user/", "user")):
        if raw.startswith(scheme_root):
            first = raw[len(scheme_root):].split("/", 1)[0]
            return namespace, first, f"{scheme_root}{first}/"
    # Unknown shape: treat the whole thing as an opaque root.
    return "resources", _VIKING_ROOT_PREFIX, f"{raw}/"


def _scope_fallback_root(config, scope: "_WorkspaceScope", endpoint: str) -> tuple[str, str]:
    """Derive (local_root, key_base) for a scope's OpenViking-fallback view.

    The root is computed with the same ``_effective_fallback_root`` identity
    hashing ``build_object_store`` uses, so the console fallback tree coincides
    with the Skill/Session backend's fallback tree on the same NAS mount.
    """
    fallback_uri = scope.root_uri
    if scope.name == "personal_workspace" and fallback_uri.rstrip("/") == "viking://user":
        fallback_uri = f"viking://user/{scope.openviking_user or 'default'}"
    elif scope.name == "team_workspace" and fallback_uri.rstrip("/") == "viking://resources":
        prefix = str(
            getattr(config, "sharing_viking_root_prefix", "") or _VIKING_ROOT_PREFIX
        ).strip().strip("/")
        fallback_uri = f"viking://resources/{prefix}"
    namespace, root_prefix, key_base = _parse_viking_uri(fallback_uri)
    shared_root = str(getattr(config, "sharing_local_root", "") or "")
    if scope.kind == "skills":
        fallback_root = str(getattr(config, "sharing_skill_local_root", "") or "") or shared_root
    else:
        fallback_root = shared_root
    root = _effective_fallback_root(
        fallback_root,
        endpoint=endpoint,
        account=str(getattr(config, "sharing_viking_account", "") or "default"),
        user=scope.openviking_user or "default",
        root_prefix=root_prefix,
        namespace=namespace,
    )
    return root, key_base


class _LocalWorkspaceStore:
    """Filesystem/NAS view of one workspace scope for OpenViking fallback.

    Backed by :class:`~teamEvolver.storage.local.LocalObjectStore` rooted at the
    same directory ``build_object_store`` derives for this scope's OpenViking
    identity, so the console and the Skill backend share one fallback tree.
    Directories are implicit (object stores are key-oriented): ``mkdir`` is a
    success no-op and listings synthesize directory entries from key prefixes.
    """

    def __init__(self, root: str, key_base: str, *, scope_root: str = "") -> None:
        self._store = LocalObjectStore(root)
        self._key_base = key_base if key_base.endswith("/") else f"{key_base}/"
        self._scope_root = str(scope_root or key_base).rstrip("/")

    def _key(self, uri: str) -> str:
        value = str(uri or "").strip().rstrip("/")
        if value == self._scope_root:
            return ""
        if value == self._key_base.rstrip("/"):
            return ""
        if value.startswith(self._key_base):
            return value[len(self._key_base):]
        if value.startswith("viking://"):
            raise _OpenVikingRequestError(404, f"NOT_FOUND: {uri}")
        # Bare keys are used internally by the local object-store adapter.
        return value.lstrip("/")

    def _uri(self, key: str) -> str:
        return f"{self._key_base}{key}"

    def read(self, uri: str) -> str:
        key = self._key(uri)
        if not key:
            raise HTTPException(status_code=400, detail="cannot read a workspace root")
        try:
            return self._store.get_object(key).read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            if is_not_found_error(exc):
                raise _OpenVikingRequestError(404, f"NOT_FOUND: {uri}") from exc
            raise

    def exists(self, uri: str) -> bool:
        key = self._key(uri)
        try:
            self._store.get_object(key)
            return True
        except Exception:  # noqa: BLE001
            prefix = f"{key}/" if key else ""
            return any(True for _ in self._store.iter_objects(prefix=prefix))

    def write(self, uri: str, content: str, mode: str) -> None:
        key = self._key(uri)
        if not key:
            raise HTTPException(status_code=400, detail="the workspace root cannot be modified")
        if mode == "create":
            try:
                self._store.get_object(key)
                raise _OpenVikingRequestError(409, f"ALREADY_EXISTS: {uri}")
            except _OpenVikingRequestError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not is_not_found_error(exc):
                    raise
        elif mode == "append":
            try:
                existing = self._store.get_object(key).read().decode("utf-8", errors="replace")
                content = existing + content
            except Exception as exc:  # noqa: BLE001
                if not is_not_found_error(exc):
                    raise
        self._store.put_object(key, content.encode("utf-8"))

    def delete(self, uri: str) -> None:
        key = self._key(uri)
        if not key:
            raise HTTPException(status_code=400, detail="the workspace root cannot be modified")
        # The target may be a file or a directory prefix (directories are
        # implicit in a key store). Try the exact key, tolerating the "it's a
        # directory / not there" case, then remove every descendant key.
        try:
            self._store.delete_object(key)
        except (IsADirectoryError, PermissionError, OSError):
            pass
        for obj in list(self._store.iter_objects(prefix=f"{key}/")):
            self._store.delete_object(obj.key)

    def entries(self, uri: str, *, recursive: bool) -> list[dict[str, Any]]:
        base_key = self._key(uri)
        prefix = f"{base_key}/" if base_key else ""
        seen_dirs: set[str] = set()
        out: list[dict[str, Any]] = []
        for obj in self._store.iter_objects(prefix=prefix):
            rest = obj.key[len(prefix):]
            if not rest:
                continue
            if recursive:
                # Emit every intermediate directory once, plus the leaf file.
                segments = rest.split("/")
                for depth in range(1, len(segments)):
                    dir_key = f"{prefix}{'/'.join(segments[:depth])}"
                    if dir_key not in seen_dirs:
                        seen_dirs.add(dir_key)
                        out.append({"uri": self._uri(dir_key), "name": segments[depth - 1], "isDir": True})
                out.append({"uri": self._uri(obj.key), "name": segments[-1], "isDir": False})
            else:
                head, _, tail = rest.partition("/")
                if tail:  # directory child
                    dir_key = f"{prefix}{head}"
                    if dir_key not in seen_dirs:
                        seen_dirs.add(dir_key)
                        out.append({"uri": self._uri(dir_key), "name": head, "isDir": True})
                else:
                    out.append({"uri": self._uri(obj.key), "name": head, "isDir": False})
        return out
