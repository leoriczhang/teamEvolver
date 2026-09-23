"""Idempotent OpenViking directory bootstrap for tenant / connection setup.

A freshly created OpenViking account — or a tenant re-pointed at another
account — starts with an empty namespace, while the console workspace, skill
sync, team-memory aggregation and session archiving all assume the layout
documented in ``docs/zh/concepts/09-storage-layout.md`` exists. This module
creates the missing skeleton and leaves every existing directory untouched.

Contract:

- ``ensure_openviking_dirs(config, account_id=...)`` walks the canonical
  directory list (shared roots + personal roots of the registered console
  users) and issues one ``POST /api/v1/fs/mkdir`` per missing parent/child,
  parents first. ``ALREADY_EXISTS`` / ``CONFLICT`` responses are reported as
  ``existing`` — existing content is never overwritten or deleted.
- Fail-open, like :func:`users_admin.ensure_openviking_account`: every
  transport / auth error is collected into the returned report instead of
  raising, so tenant creation and sharing-config saves never block on
  OpenViking availability.

Callers run this from a worker thread (``asyncio.to_thread``): the HTTP calls
here are synchronous.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import httpx

from .users_admin import (
    _load_registry,
    _openviking_endpoint,
    _openviking_root_key,
    _registry_path,
)

_LOG = logging.getLogger(__name__)

_DEFAULT_ROOT_PREFIX = "team-skill-evolver"
_DEFAULT_SHARED_KNOWLEDGE_PREFIX = "shared-knowledge"
_DEFAULT_TEAM_USER = "team"
_DEFAULT_TIMEOUT_SECONDS = 10.0
# Scope roots are owned by OpenViking itself; only their descendants are ours.
_ALLOWED_SCOPES = {"resources", "user"}
_EXISTING_TOKENS = ("ALREADY_EXISTS", "CONFLICT")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.@-]+")


def _config_str(config, field: str, default: str = "") -> str:
    return str(getattr(config, field, "") or default).strip()


def _clean_segment(value: Any) -> str:
    """Return a safe single OpenViking path segment, or ``""`` when invalid."""
    segment = str(value or "").strip().strip("/")
    if not segment or not _SEGMENT_RE.fullmatch(segment):
        return ""
    return segment


def _clean_path(value: Any) -> str:
    """Return a safe multi-segment path (``a/b``), or ``""`` when invalid."""
    segments = [part for part in str(value or "").strip().strip("/").split("/")]
    cleaned = [_clean_segment(part) for part in segments]
    if not cleaned or not all(cleaned):
        return ""
    return "/".join(cleaned)


def registered_personal_users(config) -> list[tuple[str, str]]:
    """Return ``(peer_id, personal_user)`` for every registered console user.

    ``peer_id`` isolates ``peers/<id>/skills`` in the shared resources
    namespace; ``personal_user`` owns ``viking://user/<user>/...``. They are
    usually equal, but ``personal_space.viking_user`` wins when set — exactly
    the resolution ``openviking_workspace._scope_map`` applies. The registry is
    read fail-open: an unreadable registry yields an empty list rather than
    blocking the bootstrap.
    """
    try:
        data = _load_registry(_registry_path(config), config)
    except Exception as exc:  # noqa: BLE001 - registry issues must not block bootstrap
        _LOG.warning("[openviking-dirs] cannot read the users registry: %s", exc)
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in data.get("users") or []:
        if not isinstance(raw, dict):
            continue
        peer_id = _clean_segment(raw.get("id"))
        if not peer_id or peer_id in seen:
            continue
        personal_space = raw.get("personal_space")
        personal_user = ""
        if isinstance(personal_space, dict):
            personal_user = _clean_segment(personal_space.get("viking_user"))
        seen.add(peer_id)
        pairs.append((peer_id, personal_user or peer_id))
    return pairs


def _dir_uris(config, personal_users: Iterable[tuple[str, str]]) -> list[str]:
    """Build the canonical directory list, parents before children, deduped.

    Pure function (no I/O) so the layout is unit-testable in isolation.
    """
    root_prefix = (
        _clean_path(
            _config_str(config, "sharing_viking_root_prefix") or _DEFAULT_ROOT_PREFIX
        )
        or _DEFAULT_ROOT_PREFIX
    )
    knowledge_prefix = (
        _clean_path(
            _config_str(config, "aggregation_shared_knowledge_prefix")
            or _DEFAULT_SHARED_KNOWLEDGE_PREFIX
        )
        or _DEFAULT_SHARED_KNOWLEDGE_PREFIX
    )
    team_user = _clean_segment(
        _config_str(config, "sharing_viking_user") or _DEFAULT_TEAM_USER
    ) or _DEFAULT_TEAM_USER
    team_root = f"viking://resources/{root_prefix}"
    uris: list[str] = [
        # Team resources scope root (workspace "team_resources").
        "viking://resources/team",
        # Team workspace / platform assets root, team skills and peer subtree.
        team_root,
        f"{team_root}/skills",
        f"{team_root}/peers",
    ]
    personal_user_names: list[str] = [team_user]
    for peer_id, personal_user in personal_users:
        clean_peer = _clean_segment(peer_id)
        if clean_peer:
            uris.append(f"{team_root}/peers/{clean_peer}")
            uris.append(f"{team_root}/peers/{clean_peer}/skills")
        clean_personal = _clean_segment(personal_user) or clean_peer
        if clean_personal:
            personal_user_names.append(clean_personal)
    # Aggregated team memory (workspace "team_memory").
    uris.append(f"viking://resources/{knowledge_prefix}")
    # Private namespaces: the team identity owns the aggregation work space,
    # every registered user owns their own memories/resources/skills.
    for user in personal_user_names:
        clean = _clean_segment(user)
        if not clean:
            continue
        uris.append(f"viking://user/{clean}")
        uris.extend(
            f"viking://user/{clean}/{leaf}"
            for leaf in ("memories", "resources", "skills")
        )
    return uris


def _expand_ancestors(uris: Iterable[str]) -> list[str]:
    """Expand each URI into its ancestors (parents first), scope root excluded.

    Makes the whole batch self-healing even when a prefix carries several path
    segments (``sharing_viking_root_prefix``), without ever issuing a mkdir for
    the ``viking://resources`` / ``viking://user`` scope roots themselves.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for uri in uris:
        parts = str(uri or "").strip().rstrip("/").split("/")
        # "viking://resources/team" -> ["viking:", "", "resources", "team"]
        if len(parts) < 4 or parts[0] != "viking:" or parts[2] not in _ALLOWED_SCOPES:
            continue
        scope_root = "/".join(parts[:3])
        segments = parts[3:]
        for end in range(1, len(segments) + 1):
            candidate = f"{scope_root}/{'/'.join(segments[:end])}"
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
    return ordered


def _headers(config, account_id: str, api_key: str, uri: str) -> dict[str, str]:
    """OpenViking headers for one mkdir, mirroring OpenVikingObjectStore.

    ``viking://user/<user>/...`` is a per-user namespace, so the identity
    header follows the path owner; everything else uses the configured team
    user. Trusted deployments reach both with the service key (the same
    contract the aggregation pipeline relies on).
    """
    parts = uri.split("/")
    path_user = parts[3] if uri.startswith("viking://user/") and len(parts) > 3 else ""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-OpenViking-Account": account_id or "default",
        "X-OpenViking-User": path_user
        or _config_str(config, "sharing_viking_user")
        or _DEFAULT_TEAM_USER,
        "X-OpenViking-Agent": _config_str(config, "sharing_viking_agent")
        or _DEFAULT_ROOT_PREFIX,
    }
    if api_key:
        headers["X-API-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _mkdir_outcome(response: httpx.Response) -> tuple[str, str]:
    """Classify one mkdir response as ``created`` / ``existing`` / ``error``.

    OpenViking reports failures both through the HTTP status and through a
    ``{"status": "error", ...}`` body on HTTP 200, so both must be inspected.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = ""
    server_error = False
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                detail = f"{error.get('code', 'HTTP_ERROR')}: {message}"
        if not detail and payload.get("detail"):
            detail = str(payload["detail"])
        server_error = payload.get("status") == "error"
    if not detail:
        detail = f"HTTP {response.status_code}: {(response.text or '')[:200]}"
    if any(token in detail for token in _EXISTING_TOKENS):
        # Directory already there — keep it exactly as it is.
        return "existing", detail
    if response.status_code >= 400 or server_error:
        return "error", detail
    return "created", detail


def ensure_openviking_dirs(
    config,
    *,
    account_id: str = "",
    extra_users: Iterable[str] = (),
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Create the OpenViking directory skeleton for one account (idempotent).

    ``account_id`` overrides the account from *config* (used right after a
    tenant is created, before the tenant's overrides are re-read).
    ``extra_users`` adds console user ids whose personal subdirectories must
    exist even though they are not in the registry yet.

    Returns a report — ``action`` is ``ok`` (no failures), ``partial`` (some
    directories handled, some failed) or ``failed`` (nothing could be done).
    Never raises.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    account = str(account_id or "").strip() or (
        _config_str(config, "sharing_viking_account") or "default"
    )
    result: dict[str, Any] = {
        "action": "failed",
        "account_id": account,
        "endpoint": endpoint,
        "checked": 0,
        "created": [],
        "existing": [],
        "errors": [],
    }
    if not endpoint:
        result["error"] = "OpenViking endpoint is not configured"
        _LOG.warning("[openviking-dirs] skipped for account %s: %s", account, result["error"])
        return result
    if not api_key:
        result["error"] = "OpenViking API key is not configured"
        _LOG.warning("[openviking-dirs] skipped for account %s: %s", account, result["error"])
        return result

    users = registered_personal_users(config)
    known = {peer for peer, _user in users}
    for extra in extra_users:
        clean_extra = _clean_segment(extra)
        if clean_extra and clean_extra not in known:
            users.append((clean_extra, clean_extra))
            known.add(clean_extra)
    uris = _expand_ancestors(_dir_uris(config, users))
    result["checked"] = len(uris)

    created: list[str] = []
    existing: list[str] = []
    errors: list[dict[str, str]] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            for uri in uris:
                headers = _headers(config, account, api_key, uri)
                try:
                    response = client.post(
                        f"{endpoint}/api/v1/fs/mkdir",
                        headers=headers,
                        json={"uri": uri},
                    )
                except httpx.HTTPError as exc:
                    # A transport failure means the endpoint itself is
                    # unreachable: every remaining call would pay the same
                    # timeout, so stop and let the caller report/retry.
                    errors.append({"uri": uri, "error": f"transport error: {exc}"})
                    break
                outcome, detail = _mkdir_outcome(response)
                if outcome == "created":
                    created.append(uri)
                elif outcome == "existing":
                    existing.append(uri)
                else:
                    errors.append({"uri": uri, "error": detail})
    except Exception as exc:  # noqa: BLE001 - bootstrap must never raise
        result["error"] = f"{type(exc).__name__}: {exc}"
        _LOG.warning("[openviking-dirs] client failed for account %s: %s", account, exc)

    result["created"] = created
    result["existing"] = existing
    result["errors"] = errors
    if not errors and (created or existing):
        result["action"] = "ok"
    elif created or existing:
        result["action"] = "partial"
    _LOG.info(
        "[openviking-dirs] account=%s checked=%d created=%d existing=%d errors=%d",
        account,
        len(uris),
        len(created),
        len(existing),
        len(errors),
    )
    return result