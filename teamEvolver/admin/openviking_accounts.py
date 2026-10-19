"""OpenViking account provisioning and personal Skill-space helpers.

Provisioning failures are reported without rolling back console identity changes.
OpenViking admin calls use the configured backend Root Key."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from starlette.exceptions import HTTPException

from ..config import VOLCENGINE_OPENVIKING_ENDPOINT, resolve_viking_endpoint
from ..tenants.registry import current_tenant_id
from .identity_store import IdentityStore
from .users import _slug, _upsert_user

if TYPE_CHECKING:  # platform must not import business packages at module scope
    from team_skills.library.hub import SkillHub

_LOG = logging.getLogger(__name__)

_HTTP_LOCK = threading.Lock()
_HTTP_CLIENT: httpx.Client | None = None
# Captured at import time: tests monkeypatch ``httpx.Client`` with fakes, and
# only genuine instances may enter the shared pool (a cached fake would leak
# across tests and break unrelated suites).
_REAL_HTTP_CLIENT = httpx.Client

_DEFAULT_OPENVIKING_ENDPOINT = VOLCENGINE_OPENVIKING_ENDPOINT
_DEFAULT_ACCOUNT = "default"
_DEFAULT_USER = "team"
_DEFAULT_AGENT = "teamEvolver"
_DEFAULT_ROOT_PREFIX = "teamEvolver"
_SPACES = {"personal", "team"}


def _shared_http_client() -> httpx.Client:
    """Process-wide keep-alive HTTP client for OpenViking admin calls.

    A fresh client per call forces a new DNS lookup + TCP handshake every
    time, which is prohibitively slow when the endpoint hostname resolves
    slowly (e.g. macOS.local mDNS timeout). httpx.Client is thread-safe.
    """
    global _HTTP_CLIENT
    with _HTTP_LOCK:
        client = _HTTP_CLIENT
        if type(client) is _REAL_HTTP_CLIENT and not client.is_closed:
            return client
        client = httpx.Client(timeout=10.0)
        if type(client) is _REAL_HTTP_CLIENT:
            _HTTP_CLIENT = client
        return client


def _hub_from_user(config, user: dict[str, Any], *, space: str) -> "SkillHub":
    from team_skills.library.hub import SkillHub

    from ..tenants.registry import effective_config, get_current_tenant
    config = effective_config(None, get_current_tenant(), config)
    if space not in _SPACES:
        raise HTTPException(status_code=400, detail=f"unsupported skill space: {space}")
    is_team = space == "team"
    user_id = str(user.get("id") or "")
    return SkillHub(
        backend="viking",
        endpoint="",
        customer_id="" if is_team else user_id,
        user_alias=str(user.get("display_name") or user_id or "anonymous"),
        viking_endpoint=str(getattr(config, "sharing_viking_endpoint", "") or _DEFAULT_OPENVIKING_ENDPOINT),
        viking_api_key=str(getattr(config, "sharing_viking_api_key", "") or ""),
        viking_account=str(getattr(config, "sharing_viking_account", "") or _DEFAULT_ACCOUNT),
        viking_user=str(getattr(config, "sharing_viking_user", "") or _DEFAULT_USER),
        viking_agent=str(getattr(config, "sharing_viking_agent", "") or _DEFAULT_AGENT),
        viking_root_prefix=str(getattr(config, "sharing_viking_root_prefix", "") or _DEFAULT_ROOT_PREFIX),
        viking_namespace="resources",
    )


def _copy_skills(
    *,
    source_hub: "SkillHub",
    target_hub: "SkillHub",
    requested: set[str],
) -> dict[str, Any]:
    from team_skills.library.bundle import write_skill_bundle

    manifest = source_hub._load_remote_manifest()
    if requested:
        manifest = {name: rec for name, rec in manifest.items() if name in requested}
    missing = sorted(requested - set(manifest))
    if not manifest:
        return {
            "uploaded": 0,
            "skipped": 0,
            "filtered": 0,
            "total_local": 0,
            "shared_names": [],
            "missing_names": missing,
        }

    tmp_root = tempfile.mkdtemp(prefix="teamEvolver_user_share_")
    try:
        tmp_skills = os.path.join(tmp_root, "skills")
        os.makedirs(tmp_skills, exist_ok=True)
        names: list[str] = []
        for name, rec in sorted(manifest.items()):
            bundle = source_hub._download_skill_bundle(name, rec)
            write_skill_bundle(os.path.join(tmp_skills, name), bundle, clean=True)
            names.append(name)
        from team_skills.library.mutations import (
            SkillMutationCommand,
            SkillMutationService,
        )

        service = SkillMutationService.from_hub(target_hub)
        commits = [
            service.execute(
                SkillMutationCommand(
                    action="publish",
                    name=name,
                    mutation_id=f"user-share-{uuid.uuid4().hex}",
                    skills_dir=tmp_skills,
                )
            )
            for name in names
        ]
        result = {
            "uploaded": len(commits),
            "skipped": 0,
            "filtered": 0,
            "total_local": len(names),
            "event_ids": [item["event_id"] for item in commits],
        }
        result["shared_names"] = names
        result["missing_names"] = missing
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _read_hub_skill(hub: SkillHub, name: str) -> dict[str, Any] | None:
    """Return ``{name, description, category, body, skill_md, files}`` or None.

    Reads a single skill's bundle straight from the hub's object storage so the
    personal-space editor can round-trip content without a local skills dir.
    """
    from team_skills.library.frontmatter import parse_skill_md_text

    manifest = hub._load_remote_manifest()
    record = manifest.get(name)
    if not isinstance(record, dict):
        return None
    bundle = hub._download_skill_bundle(name, record)
    skill_md = bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
    parsed = parse_skill_md_text(skill_md) or {}
    body = skill_md.split("---", 2)[-1].strip() if skill_md.count("---") >= 2 else skill_md
    return {
        "name": name,
        "description": str(parsed.get("description") or record.get("description") or ""),
        "category": str(record.get("category") or "general"),
        "body": body,
        "skill_md": skill_md,
        "files": sorted(bundle.keys()),
    }


def _publish_skill_to_hub(
    target_hub: SkillHub,
    *,
    name: str,
    skill_md: str,
    mutation_prefix: str,
) -> dict[str, Any]:
    """Write a single SKILL.md into ``target_hub`` via the mutation service."""
    from team_skills.library.mutations import SkillMutationCommand, SkillMutationService

    tmp_root = tempfile.mkdtemp(prefix="teamEvolver_skill_edit_")
    try:
        skill_dir = os.path.join(tmp_root, "skills", name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(skill_md)
        service = SkillMutationService.from_hub(target_hub)
        return service.execute(
            SkillMutationCommand(
                action="publish",
                name=name,
                mutation_id=f"{mutation_prefix}-{uuid.uuid4().hex}",
                skills_dir=os.path.join(tmp_root, "skills"),
            )
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _publish_request_bucket(config):
    """Return the shared object-store bucket for publish requests, or None."""
    from team_skills.library.hub import SkillHub

    hub = SkillHub.object_storage_from_config(config, tenant_id=current_tenant_id())
    return hub._bucket if hub is not None else None


def _publish_request_key(request_id: str) -> str:
    return f"skill_publish_requests/{_slug(request_id)}.json"


def _load_publish_requests(config) -> list[dict[str, Any]]:
    bucket = _publish_request_bucket(config)
    if bucket is None:
        return []
    from ..storage import is_not_found_error

    rows: list[dict[str, Any]] = []
    for obj in bucket.iter_objects(prefix="skill_publish_requests/"):
        key = str(getattr(obj, "key", "") or "")
        if not key.endswith(".json"):
            continue
        try:
            raw = bucket.get_object(key).read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - skip unreadable request blobs
            if is_not_found_error(exc):
                continue
            raise
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows


def _save_publish_request(config, record: dict[str, Any]) -> None:
    bucket = _publish_request_bucket(config)
    if bucket is None:
        raise HTTPException(
            status_code=503,
            detail="OpenViking object storage is not configured for publish requests",
        )
    bucket.put_object(
        _publish_request_key(str(record.get("request_id") or "")),
        json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _openviking_endpoint(config) -> str:
    return resolve_viking_endpoint(
        str(getattr(config, "sharing_viking_deployment", "") or "cloud"),
        str(getattr(config, "sharing_viking_endpoint", "") or ""),
    ).rstrip("/")


def _openviking_root_key(config) -> str:
    return str(getattr(config, "sharing_viking_api_key", "") or "").strip()


def list_openviking_accounts(config) -> dict[str, Any]:
    """List OpenViking accounts (workspaces) via the ROOT admin API.

    Fail-open: any transport/auth error is swallowed and the currently
    configured account is returned as the single fallback, so user
    registration never breaks when OpenViking is unreachable.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    current = str(getattr(config, "sharing_viking_account", "") or "default").strip() or "default"
    result: dict[str, Any] = {
        "accounts": [current],
        "current": current,
        "source": "fallback",
        "endpoint": endpoint,
    }
    deployment = str(getattr(config, "sharing_viking_deployment", "") or "cloud").strip().lower()
    if endpoint and deployment == "local":
        # A trusted-mode local server intentionally disables the admin API.
        # Its single configured account remains usable through account-scoped
        # resource APIs without an API key. Ignore any retained cloud key so a
        # reversible cloud/local switch does not make the local admin probe
        # produce a misleading 403.
        result["source"] = "trusted-local"
        return result
    if not endpoint or not api_key:
        result["error"] = "OpenViking endpoint or API key not configured"
        return result
    url = f"{endpoint}/api/v1/admin/accounts"
    headers = {
        "Accept": "application/json",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    try:
        response = _shared_http_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        _LOG.warning("openviking list accounts failed: %s", exc)
        return result
    if response.status_code != 200:
        result["error"] = f"HTTP {response.status_code}: {(response.text or '')[:400]}"
        _LOG.warning("openviking list accounts failed: %s", result["error"])
        return result
    try:
        payload = response.json()
    except ValueError:
        result["error"] = "invalid JSON from OpenViking"
        return result
    rows = payload.get("result") if isinstance(payload, dict) else None
    accounts: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            account_id = str((row or {}).get("account_id") or "").strip() if isinstance(row, dict) else ""
            if account_id and account_id not in accounts:
                accounts.append(account_id)
    if current and current not in accounts:
        accounts.append(current)
    if accounts:
        result["accounts"] = sorted(accounts)
        result["source"] = "openviking"
    return result


def ensure_openviking_account(config, account_id: str, admin_user: str = "team") -> dict[str, Any]:
    """Ensure an OpenViking account (workspace) exists for a tenant.

    The tenant_id is used directly as the OpenViking account_id (the identity
    contract in ``tenants.registry.effective_config``). If the account already
    exists it is reused; otherwise it is created with ``admin_user`` as the
    first admin user. This mirrors the existing ``sync_openviking_user`` and
    ``list_openviking_accounts`` helpers and is fail-open: any transport/auth
    error is swallowed and returned as ``error`` so tenant creation in PG never
    blocks on OpenViking availability. The response carries an ``action`` of
    ``bound`` / ``created`` / ``failed`` so the console can surface the result.

    In ``trusted`` (local) deployments the create response does not include a
    ``user_key``; access still works via the configured service key plus the
    ``X-OpenViking-Account``/``X-OpenViking-User`` headers, which is exactly
    how ``OpenVikingWorkspaceMixin`` and ``OpenVikingObjectStore`` reach it.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    admin_slug = _slug(str(admin_user or "team")) or "team"
    account = str(account_id or "").strip()
    result: dict[str, Any] = {
        "account_id": account,
        "admin_user": admin_slug,
        "action": "failed",
        "endpoint": endpoint,
    }
    if not account:
        result["error"] = "account_id is required"
        return result
    if not endpoint or not api_key:
        result["error"] = "OpenViking endpoint or API key not configured"
        _LOG.warning("openviking ensure account skipped for %s: %s", account, result["error"])
        return result

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }

    # 1. Does the account already exist? Reuse list_openviking_accounts so the
    #    same fail-open fallback and parsing apply. A list failure is treated
    #    as "unknown — try to create" rather than a hard error, because the
    #    create call below will surface a real conflict (409) if it exists.
    listing = list_openviking_accounts(config)
    existing = {str(a) for a in (listing.get("accounts") or [])}
    if account in existing:
        result["action"] = "bound"
        result["source"] = listing.get("source", "openviking")
        _LOG.info("[openviking] tenant %s bound to existing account", account)
        # Still ensure the team admin user exists under the account.
        _ensure_account_admin_user(config, account, admin_slug, headers)
        return result

    # 2. Create the account with the first admin user.
    url = f"{endpoint}/api/v1/admin/accounts"
    body = {"account_id": account, "admin_user_id": admin_slug}
    try:
        response = _shared_http_client().post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        _LOG.warning("openviking create account failed for %s: %s", account, exc)
        return result
    text = (response.text or "")[:400]
    if response.status_code in (200, 201):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        result["action"] = "created"
        result["response"] = payload
        _LOG.info("[openviking] created account %s for tenant", account)
        return result
    # 409 / "already exists" → the account is there (race or list missed it),
    # treat as bound and make sure the admin user exists.
    if response.status_code == 409 or "already" in text.lower() or "exists" in text.lower():
        result["action"] = "bound"
        result["conflict"] = True
        _LOG.info("[openviking] account %s already existed (conflict), binding", account)
        _ensure_account_admin_user(config, account, admin_slug, headers)
        return result
    result["error"] = f"HTTP {response.status_code}: {text}"
    _LOG.warning("openviking create account failed for %s: %s", account, result["error"])
    return result


def _ensure_account_admin_user(
    config,
    account: str,
    user_id: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Ensure ``user_id`` exists under ``account``; tolerate trusted-mode no-key.

    Read-then-write idempotency: list users first (best-effort), then POST only
    if the user is missing. Any transport/auth error is swallowed so a missing
    user never blocks account binding.
    """
    endpoint = _openviking_endpoint(config)
    result: dict[str, Any] = {"user_id": user_id, "synced": False, "already_exists": False}
    if not endpoint:
        result["error"] = "endpoint not configured"
        return result

    # Best-effort list to avoid a redundant create that 409s.
    list_url = f"{endpoint}/api/v1/admin/accounts/{account}/users"
    try:
        list_resp = _shared_http_client().get(list_url, headers=headers)
        if list_resp.status_code == 200:
            try:
                rows = list_resp.json().get("result") or []
            except ValueError:
                rows = []
            if any(
                isinstance(r, dict) and str(r.get("user_id") or "") == user_id
                for r in rows
            ):
                result["already_exists"] = True
                result["synced"] = True
                return result
    except httpx.HTTPError as exc:
        _LOG.debug("openviking list users for %s failed (will try create): %s", account, exc)

    # Register the user (idempotent on OV's side via 409).
    try:
        create_resp = _shared_http_client().post(
            list_url,
            headers=headers,
            json={"user_id": user_id, "role": "admin"},
        )
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        return result
    if create_resp.status_code in (200, 201):
        result["synced"] = True
        try:
            result["response"] = create_resp.json()
        except ValueError:
            pass
        return result
    text = (create_resp.text or "")[:400]
    if create_resp.status_code == 409 or "already" in text.lower() or "exists" in text.lower():
        result["already_exists"] = True
        result["synced"] = True
        return result
    result["error"] = f"HTTP {create_resp.status_code}: {text}"
    return result


def _map_openviking_role(value: Any) -> str:
    """External account roles never grant platform authority."""
    role = str(value or "user").strip().lower()
    return "tenant_admin" if role == "admin" else "member"


def list_openviking_account_users(config, account: str) -> dict[str, Any]:
    """List the users registered under one OpenViking account via the ROOT API.

    Fail-open: any transport/auth error is swallowed and returned as ``error``
    with an empty ``users`` list, so the console can still render the account
    picker when OpenViking is unreachable. This never provisions or mutates
    anything; it is a read-only reflection of OpenViking's own account users.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    account_id = str(account or "").strip() or str(
        getattr(config, "sharing_viking_account", "") or "default"
    ).strip() or "default"
    result: dict[str, Any] = {
        "account": account_id,
        "users": [],
        "source": "fallback",
        "endpoint": endpoint,
    }
    if not endpoint or not api_key:
        result["error"] = "OpenViking endpoint or API key not configured"
        return result
    url = f"{endpoint}/api/v1/admin/accounts/{account_id}/users"
    headers = {
        "Accept": "application/json",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    try:
        response = _shared_http_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        _LOG.warning("openviking list account users failed: %s", exc)
        return result
    if response.status_code != 200:
        result["error"] = f"HTTP {response.status_code}: {(response.text or '')[:400]}"
        _LOG.warning("openviking list account users failed: %s", result["error"])
        return result
    try:
        payload = response.json()
    except ValueError:
        result["error"] = "invalid JSON from OpenViking"
        return result
    rows = payload.get("result") if isinstance(payload, dict) else None
    users: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            user_id = str(row.get("user_id") or "").strip()
            if not user_id:
                continue
            users.append(
                {
                    "user_id": user_id,
                    "role": _map_openviking_role(row.get("role")),
                    "openviking_role": str(row.get("role") or "user"),
                }
            )
    users.sort(key=lambda item: item["user_id"])
    result["users"] = users
    result["source"] = "openviking"
    return result


def import_openviking_account_users(
    config,
    account: str,
    user_ids: list[str],
) -> dict[str, Any]:
    """Import account identities without merging unrelated same-name users."""
    account_id = str(account or "").strip() or str(
        getattr(config, "sharing_viking_account", "") or "default"
    ).strip() or "default"
    available = {
        row["user_id"]: row
        for row in list_openviking_account_users(config, account_id).get("users") or []
    }
    requested = [str(uid or "").strip() for uid in (user_ids or []) if str(uid or "").strip()]

    imported: list[str] = []
    skipped_existing: list[str] = []
    missing: list[str] = []
    store = IdentityStore(config)
    tenant = current_tenant_id()
    membership_updates = []
    def edit(data):
        existing = {u["id"]: u for u in data["users"]}
        for raw_id in requested:
            row = available.get(raw_id)
            if row is None:
                missing.append(raw_id)
                continue
            user_id = f"{tenant}.{_slug(raw_id)}"
            identity = {"provider": "openviking", "account": account_id, "user_id": raw_id}
            if user_id in existing:
                if existing[user_id].get("external_identity") != identity:
                    raise HTTPException(409, f"IMPORT_ID_COLLISION: {user_id}")
                skipped_existing.append(user_id)
                continue
            created = _upsert_user(data, {"id": user_id, "display_name": raw_id,
                                         "personal_space": {"viking_user": raw_id}}, config=config)
            created["external_identity"] = identity
            created["personal_space"]["viking_user"] = raw_id
            existing[user_id] = created
            membership_updates.append({
                "tenant_id": tenant, "user_id": user_id, "role": row["role"], "status": "active",
                "created_at": time.time(), "created_by": "openviking-import",
            })
            imported.append(user_id)
    store.edit_users(edit, actor="openviking-import", action="user.import", membership_updates=membership_updates)
    return {
        "account": account_id,
        "imported": sorted(imported),
        "skipped_existing": sorted(skipped_existing),
        "missing": sorted(missing),
    }


def sync_openviking_user(config, user_id: str) -> dict[str, Any]:
    """Ensure a same-name OpenViking user exists under the default account.

    Fail-open: any transport / auth error is logged and swallowed so
    teamEvolver user creation is never blocked by OpenViking availability.
    Studio visibility is a downstream nice-to-have, not a hard dependency.
    """
    endpoint = _openviking_endpoint(config)
    api_key = _openviking_root_key(config)
    account = str(getattr(config, "sharing_viking_account", "") or "default").strip() or "default"
    slug = _slug(str(user_id or ""))
    result: dict[str, Any] = {
        "account_id": account,
        "user_id": slug,
        "synced": False,
        "already_exists": False,
        "endpoint": endpoint,
    }
    if not endpoint or not api_key:
        result["error"] = "OpenViking endpoint or API key not configured"
        _LOG.warning("openviking sync skipped for %s: %s", slug, result["error"])
        return result
    url = f"{endpoint}/api/v1/admin/accounts/{account}/users"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    body = {"user_id": slug, "role": "user"}
    try:
        response = _shared_http_client().post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        result["error"] = f"transport error: {exc}"
        _LOG.warning("openviking sync failed for %s: %s", slug, exc)
        return result
    if response.status_code in (200, 201):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        result["synced"] = True
        result["response"] = payload
        return result
    text = (response.text or "")[:400]
    if response.status_code == 409 or "already" in text.lower() or "exists" in text.lower():
        result["already_exists"] = True
        result["synced"] = True
        return result
    result["error"] = f"HTTP {response.status_code}: {text}"
    _LOG.warning("openviking sync failed for %s: %s", slug, result["error"])
    return result
