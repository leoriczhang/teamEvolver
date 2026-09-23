"""FastAPI application and route wiring for the teamEvolver service.

``RoutesMixin`` builds the ``FastAPI`` app and its endpoints (console,
health, skill/user admin, model settings, and internal skill reload). Route bodies delegate to the owning
:class:`~teamEvolver.proxy.server.ProxyServer` instance.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from team_miner.lifecycle import (
    MiningLifecycleError,
    list_mined_skill_statuses,
    resolve_mined_job_skill_root,
    submit_mined_skill,
)
from team_replay.aggregation import aggregate_true_replay_windows
from team_replay.policy import (
    aggregate_case_checklists,
    progressive_replay_decision,
    select_replay_cases,
)
from team_skills.candidates.feedback import CandidateFeedbackStore, FeedbackConflictError
from team_skills.candidates.store import ValidationStore
from team_skills.evolution.experience_library import ExperienceLibraryStore
from team_skills.library.bundle import (
    bundle_entrypoint_bytes,
    bundle_tree_sha256,
    candidate_skill_bundle,
    read_skill_bundle,
    write_skill_bundle,
)
from team_skills.library.hub import SkillHub
from team_skills.library.mutations import SkillMutationService
from team_skills.library.render import build_skill_md

try:
    from team_skills.evolution.runtime.mixins import EvolveEngineMixin
except ModuleNotFoundError as exc:
    if not str(exc.name or "").startswith("team_skills.evolution.runtime"):
        raise
    EvolveEngineMixin = None

from ..config import (
    LOCAL_OPENVIKING_ENDPOINT,
    VOLCENGINE_OPENVIKING_ENDPOINT,
)
from ..config_store import ConfigStore
from ..integrations.agent_protocol import is_v1_payload
from ..integrations.agent_registry import (
    list_agents,
    public_agent_record,
    register_agent,
)
from ..session_store import SessionStore, session_display_meta
from ..storage import (
    LocalObjectStore,
    PgObjectStore,
    is_not_found_error,
    normalize_backend,
)
from ..tenants.registry import (
    AGENT_TOKEN_PREFIX,
    DEFAULT_TENANT_ID,
    RETIRED_AGENT_TOKEN_PREFIX,
    current_tenant_id,
    effective_config,
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)
from .tenant_routes import get_tenant_registry, register_tenant_routes
from .users_admin import (
    _find_user,
    _load_registry,
    _public_user,
    _registry_path,
    _save_registry,
    _upsert_user,
    _verify_password,
    sync_agent_subject_mappings,
    sync_openviking_user,
)
from .viking_dirs import ensure_openviking_dirs

logger = logging.getLogger(__name__)
_INSTANCE_ID = (
    os.environ.get("TEAMEVOLVER_INSTANCE_ID")
    or os.environ.get("HOSTNAME")
    or f"{socket.gethostname()}:{os.getpid()}"
)
_SESSION_COOKIE = "teamEvolver_console_session"
_SESSION_TTL_SECONDS = 24 * 60 * 60
_SESSION_RENEW_INTERVAL_SECONDS = 5 * 60
# Console sessions are cached per process while the authoritative store is
# shared (PG objects in PG mode, the registry directory otherwise), so a login
# handled by one instance is unknown to its peers. A cache miss re-reads the
# store instead of rejecting the caller — throttled per token so a stale or
# forged cookie cannot hammer it.
_SESSION_MISS_REFRESH_SECONDS = 2.0
_SESSION_MISS_CACHE_LIMIT = 4096
_console_session_miss_lock = threading.Lock()
_console_session_misses: dict[str, float] = {}
_DASHBOARD_CACHE: dict[str, tuple[float, Any]] = {}


def _scoped_cache_key(key: str) -> str:
    """Namespace a dashboard cache key by the request's tenant.

    Keeps tenant A's cached conversation/queue rows from ever being served to
    tenant B. No-op in default-tenant/background contexts, so single-tenant
    behavior (and cache keys) is unchanged.
    """
    tenant = current_tenant_id()
    return key if tenant == DEFAULT_TENANT_ID else f"t:{tenant}|{key}"


def _tenant_effective_config(owner, config=None):
    """Request-scoped effective config (multi-tenancy plan §1.3).

    Merges the current tenant's config overrides over the passed config
    (default: the server's global config) so request-context stores
    (SessionStore / SkillHub) honor per-tenant backend selection. Returns the
    input unchanged for the default tenant or when no registry is available.
    """
    base = config if config is not None else owner.config
    try:
        ctx = get_current_tenant()
    except Exception:  # noqa: BLE001 - contextvar not set (background job)
        return base
    return effective_config(None, ctx, base)


def _cached_dashboard_value(key: str, ttl_seconds: float, loader):
    key = _scoped_cache_key(key)
    now = time.monotonic()
    cached = _DASHBOARD_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    value = loader()
    _DASHBOARD_CACHE[key] = (now + max(0.1, ttl_seconds), value)
    return value


def _invalidate_dashboard_cache(*prefixes: str) -> None:
    if not prefixes:
        _DASHBOARD_CACHE.clear()
        return
    prefixes = [_scoped_cache_key(prefix) for prefix in prefixes]
    for key in list(_DASHBOARD_CACHE):
        if any(key.startswith(prefix) for prefix in prefixes):
            _DASHBOARD_CACHE.pop(key, None)


def _pg_config_get(owner, key: str):
    """Read a JSON value from PG ``config_kv``; ``None`` if PG not enabled."""
    if not bool(getattr(owner.config, "storage_pg_enabled", False)):
        return None
    try:
        from ..storage.pg_pool import get_pg_runtime, dsn_from_env
        dsn = str(getattr(owner.config, "storage_pg_dsn", "") or "")
        if not dsn:
            dsn = dsn_from_env()
        if not dsn:
            return None
        schema = str(getattr(owner.config, "storage_pg_schema", "teamevolver"))
        rt = get_pg_runtime(
            dsn=dsn,
            schema=schema,
            pool_min=int(getattr(owner.config, "storage_pg_pool_min", 2)),
            pool_max=int(getattr(owner.config, "storage_pg_pool_max", 20)),
            command_timeout=float(getattr(owner.config, "storage_pg_command_timeout_seconds", 30.0)),
            ssl=str(getattr(owner.config, "storage_pg_ssl", "prefer")),
        )
        return rt.config_get(key)
    except Exception:  # noqa: BLE001
        return None


def _pg_config_put(owner, key: str, value) -> bool:
    """Write a JSON value to PG ``config_kv``; ``False`` if PG not enabled."""
    if not bool(getattr(owner.config, "storage_pg_enabled", False)):
        return False
    try:
        from ..storage.pg_pool import get_pg_runtime, dsn_from_env
        dsn = str(getattr(owner.config, "storage_pg_dsn", "") or "")
        if not dsn:
            dsn = dsn_from_env()
        if not dsn:
            return False
        schema = str(getattr(owner.config, "storage_pg_schema", "teamevolver"))
        rt = get_pg_runtime(
            dsn=dsn,
            schema=schema,
            pool_min=int(getattr(owner.config, "storage_pg_pool_min", 2)),
            pool_max=int(getattr(owner.config, "storage_pg_pool_max", 20)),
            command_timeout=float(getattr(owner.config, "storage_pg_command_timeout_seconds", 30.0)),
            ssl=str(getattr(owner.config, "storage_pg_ssl", "prefer")),
        )
        return rt.config_put(key, value)
    except Exception:  # noqa: BLE001
        return False


def _model_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    llm = store_data.get("llm") if isinstance(store_data.get("llm"), dict) else {}
    api_key = str(getattr(config, "llm_api_key", "") or llm.get("api_key") or "")
    temperature = (
        getattr(config, "llm_temperature", 0.0)
        if getattr(config, "llm_temperature", None) is not None
        else llm.get("temperature", 0.4)
    )
    tenant_id = current_tenant_id()
    return {
        "tenant_id": tenant_id,
        "scope": "global" if tenant_id == DEFAULT_TENANT_ID else "tenant",
        "provider": str(getattr(config, "llm_provider", "") or llm.get("provider") or "custom"),
        "base_url": str(getattr(config, "llm_api_base", "") or llm.get("api_base") or ""),
        "model": str(getattr(config, "llm_model_id", "") or llm.get("model_id") or ""),
        "max_tokens": int(getattr(config, "llm_max_tokens", 0) or llm.get("max_tokens") or 100000),
        "temperature": float(temperature),
        "max_concurrency": int(
            getattr(config, "llm_max_concurrency", 0)
            or llm.get("max_concurrency")
            or 8
        ),
        "queue_capacity": int(
            getattr(config, "llm_queue_capacity", 0)
            or llm.get("queue_capacity")
            or 64
        ),
        "api_key_present": bool(api_key),
    }


def _team_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    team = (
        store_data.get("team")
        if isinstance(store_data.get("team"), dict)
        else {}
    )
    configured = str(team.get("display_name") or "Team").strip() or "Team"
    override_source = ""
    environment_override = str(
        os.environ.get("EVOLVE_TEAM_DISPLAY_NAME") or ""
    ).strip()
    if environment_override:
        override_source = "EVOLVE_TEAM_DISPLAY_NAME"
    effective = str(
        environment_override
        or getattr(config, "team_display_name", "")
        or configured
        or "Team"
    ).strip()
    return {
        "display_name": effective or "Team",
        "configured_display_name": configured,
        "environment_override": environment_override,
        "override_source": override_source,
    }


def _evolve_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    """Return the tunable evolution/validation process without secret values."""
    evolve = (
        store_data.get("evolve")
        if isinstance(store_data.get("evolve"), dict)
        else {}
    )
    validation = (
        store_data.get("validation")
        if isinstance(store_data.get("validation"), dict)
        else {}
    )
    dreamcycle = (
        store_data.get("dreamcycle")
        if isinstance(store_data.get("dreamcycle"), dict)
        else {}
    )
    environment_overrides = {
        name: str(os.environ[name])
        for name in (
            "EVOLVE_TEAM_DISPLAY_NAME",
            "EVOLVE_MODEL",
            "EVOLVE_LLM_MAX_TOKENS",
            "EVOLVE_LLM_TEMPERATURE",
            "EVOLVE_USE_SESSION_JUDGE",
            "EVOLVE_PUBLISH_MODE",
            "EVOLVE_VALIDATION_MAX_REJECTIONS",
            "EVOLVE_HUMAN_REVIEW_ENABLED",
            "EVOLVE_HUMAN_REVIEW_TIMEOUT_SECONDS",
            "EVOLVE_INTERVAL",
            "EVOLVE_EVIDENCE_ENABLED",
            "EVOLVE_EVIDENCE_MAX_ENTRIES",
            "EVOLVE_EVIDENCE_RECENT_LIMIT",
            "EVOLVE_EVIDENCE_HISTORICAL_LIMIT",
            "EVOLVE_EVIDENCE_REPLAY_CASES_PER_WINDOW",
            "EVOLVE_EVIDENCE_CHANGE_DEBT_THRESHOLD",
            "EVOLVE_DATASET_SYNTHESIS_ENABLED",
            "EVOLVE_DATASET_TEST_CASES",
            "EVOLVE_DATASET_MIN_REQUIREMENTS",
            "EVOLVE_DATASET_MAX_REQUIREMENTS",
            "EVOLVE_DATASET_DISCLOSURE_BATCH_SIZE",
            "EVOLVE_CANDIDATE_COALESCE_ENABLED",
        )
        if name in os.environ
    }
    return {
        "environment_overrides": environment_overrides,
        "evolve": {
            "use_session_judge": bool(
                evolve.get(
                    "use_session_judge",
                    getattr(config, "evolve_use_session_judge", True),
                )
            ),
            "publish_mode": str(
                evolve.get("publish_mode")
                or getattr(config, "evolve_publish_mode", "validated")
                or "validated"
            ),
            "validation_max_rejections": int(
                evolve.get("validation_max_rejections")
                or getattr(
                    config,
                    "evolve_validation_max_rejections",
                    1,
                )
                or 1
            ),
            "human_review_enabled": bool(
                evolve.get(
                    "human_review_enabled",
                    getattr(config, "evolve_human_review_enabled", True),
                )
            ),
            "human_review_timeout_seconds": int(
                evolve.get("human_review_timeout_seconds")
                or getattr(
                    config,
                    "evolve_human_review_timeout_seconds",
                    86400,
                )
                or 86400
            ),
            "interval_seconds": int(
                evolve.get("interval_seconds")
                or getattr(config, "evolve_interval_seconds", 600)
                or 600
            ),
            "evidence_enabled": bool(
                evolve.get(
                    "evidence_enabled",
                    getattr(config, "evolve_evidence_enabled", True),
                )
            ),
            "evidence_max_entries": int(
                evolve.get("evidence_max_entries")
                or getattr(config, "evolve_evidence_max_entries", 400)
                or 400
            ),
            "evidence_recent_limit": int(
                evolve.get("evidence_recent_limit")
                or getattr(config, "evolve_evidence_recent_limit", 20)
                or 20
            ),
            "evidence_historical_limit": int(
                evolve.get(
                    "evidence_historical_limit",
                    getattr(config, "evolve_evidence_historical_limit", 20),
                )
                or 0
            ),
            "evidence_replay_cases_per_window": int(
                evolve.get("evidence_replay_cases_per_window")
                or getattr(
                    config,
                    "evolve_evidence_replay_cases_per_window",
                    1,
                )
                or 1
            ),
            "evidence_change_debt_threshold": int(
                evolve.get("evidence_change_debt_threshold")
                or getattr(
                    config,
                    "evolve_evidence_change_debt_threshold",
                    3,
                )
                or 3
            ),
            "dataset_synthesis_enabled": bool(
                evolve.get(
                    "dataset_synthesis_enabled",
                    getattr(config, "evolve_dataset_synthesis_enabled", True),
                )
            ),
            "dataset_test_cases": int(
                evolve.get("dataset_test_cases")
                or getattr(config, "evolve_dataset_test_cases", 2)
                or 2
            ),
            "dataset_min_requirements": int(
                evolve.get("dataset_min_requirements")
                or getattr(config, "evolve_dataset_min_requirements", 12)
                or 12
            ),
            "dataset_max_requirements": int(
                evolve.get("dataset_max_requirements")
                or getattr(config, "evolve_dataset_max_requirements", 24)
                or 24
            ),
            "dataset_disclosure_batch_size": int(
                evolve.get("dataset_disclosure_batch_size")
                or getattr(
                    config,
                    "evolve_dataset_disclosure_batch_size",
                    4,
                )
                or 4
            ),
            "candidate_coalesce_enabled": bool(
                evolve.get(
                    "candidate_coalesce_enabled",
                    getattr(
                        config,
                        "evolve_candidate_coalesce_enabled",
                        True,
                    ),
                )
            ),
            "bundle_text_extensions": list(
                getattr(
                    config,
                    "evolve_bundle_text_extensions",
                    evolve.get("bundle_text_extensions") or [".py", ".sh"],
                )
                or [".py", ".sh"]
            ),
            "bundle_max_file_bytes": int(
                evolve.get("bundle_max_file_bytes")
                or getattr(config, "evolve_bundle_max_file_bytes", 262144)
                or 262144
            ),
            "bundle_max_prompt_bytes": int(
                evolve.get("bundle_max_prompt_bytes")
                or getattr(config, "evolve_bundle_max_prompt_bytes", 786432)
                or 786432
            ),
            "bundle_allow_delete": bool(
                evolve.get(
                    "bundle_allow_delete",
                    getattr(config, "evolve_bundle_allow_delete", True),
                )
            ),
            "bundle_static_checks_enabled": bool(
                evolve.get(
                    "bundle_static_checks_enabled",
                    getattr(
                        config,
                        "evolve_bundle_static_checks_enabled",
                        True,
                    ),
                )
            ),
        },
        "validation": {
            "enabled": bool(
                validation.get(
                    "enabled",
                    getattr(config, "validation_enabled", True),
                )
            ),
            "mode": str(
                validation.get("mode")
                or getattr(config, "validation_mode", "true_replay")
                or "true_replay"
            ),
            "idle_after_seconds": int(
                validation.get("idle_after_seconds")
                or getattr(config, "validation_idle_after_seconds", 300)
                or 300
            ),
            "poll_interval_seconds": int(
                validation.get("poll_interval_seconds")
                or getattr(config, "validation_poll_interval_seconds", 60)
                or 60
            ),
            "max_jobs_per_day": int(
                validation.get(
                    "max_jobs_per_day",
                    getattr(config, "validation_max_jobs_per_day", 5),
                )
                or 0
            ),
            "max_concurrency": int(
                validation.get("max_concurrency")
                or getattr(config, "validation_max_concurrency", 1)
                or 1
            ),
            "required_results": int(
                validation.get("required_results")
                or getattr(config, "validation_required_results", 3)
                or 3
            ),
            "required_approvals": int(
                validation.get("required_approvals")
                or getattr(config, "validation_required_approvals", 2)
                or 2
            ),
        },
        "memory_maintenance": {
            "enabled": bool(
                dreamcycle.get(
                    "enabled",
                    getattr(config, "dreamcycle_enabled", False),
                )
            ),
            "auto_start": bool(
                dreamcycle.get(
                    "auto_start",
                    getattr(config, "dreamcycle_auto_start", False),
                )
            ),
            "engine": "teamEvolver-native-dreamcycle",
            "full_capabilities": True,
            "agent_id": str(
                getattr(config, "sharing_viking_user", "") or ""
            ),
            "customer_id": str(
                dreamcycle.get("customer_id")
                or dreamcycle.get("peer_id")
                or ""
            ),
            "maintained_space": (
                (
                    "viking://user/peers/"
                    f"{dreamcycle.get('customer_id') or dreamcycle.get('peer_id')}/"
                    "memories/"
                )
                if dreamcycle.get("customer_id") or dreamcycle.get("peer_id")
                else "viking://user/memories/"
            ),
            "embed_model": "",
            "embed_base_url": "",
            "embed_api_key_present": False,
            "semantic_dedup_enabled": False,
            "dedup_merge_threshold": float(
                dreamcycle.get("dedup_merge_threshold", 0.86)
                if dreamcycle.get("dedup_merge_threshold") is not None
                else 0.86
            ),
            "dedup_warn_threshold": float(
                dreamcycle.get("dedup_warn_threshold", 0.72)
                if dreamcycle.get("dedup_warn_threshold") is not None
                else 0.72
            ),
            "tools": [
                "viking_search",
                "viking_read",
                "viking_read_many",
                "viking_browse",
                "viking_remember",
                "viking_forget",
                "viking_merge",
                "list_customers",
                "memory_audit",
                "memory_sanitize",
                "save_report",
                "shared_notes",
            ],
        },
    }




def _langfuse_tracing_settings_payload(
    config,
    store_data: dict[str, Any],
) -> dict[str, Any]:
    """Service-wide outbound tracing settings, separate from tenant sources."""
    langfuse = (
        store_data.get("langfuse")
        if isinstance(store_data.get("langfuse"), dict)
        else {}
    )
    from ..observability import langfuse_status

    public_key = str(
        langfuse.get("tracing_public_key")
        or getattr(config, "langfuse_tracing_public_key", "")
        or ""
    )
    secret_key = str(
        langfuse.get("tracing_secret_key")
        or getattr(config, "langfuse_tracing_secret_key", "")
        or ""
    )
    return {
        "enabled": bool(
            langfuse.get(
                "tracing_enabled",
                getattr(config, "langfuse_tracing_enabled", False),
            )
        ),
        "host": str(
            langfuse.get("tracing_host")
            or getattr(config, "langfuse_tracing_host", "")
            or ""
        ),
        "public_key_present": bool(public_key),
        "secret_key_present": bool(secret_key),
        "environment": str(
            langfuse.get("tracing_environment")
            or getattr(config, "langfuse_tracing_environment", "")
            or "local"
        ),
        "release": str(
            langfuse.get("tracing_release")
            or getattr(config, "langfuse_tracing_release", "")
            or ""
        ),
        "sample_rate": float(
            langfuse.get(
                "tracing_sample_rate",
                getattr(config, "langfuse_tracing_sample_rate", 1.0),
            )
        ),
        "capture_content": bool(
            langfuse.get(
                "tracing_capture_content",
                getattr(config, "langfuse_tracing_capture_content", True),
            )
        ),
        "flush_at": int(
            langfuse.get(
                "tracing_flush_at",
                getattr(config, "langfuse_tracing_flush_at", 1),
            )
        ),
        "flush_interval_seconds": float(
            langfuse.get(
                "tracing_flush_interval_seconds",
                getattr(
                    config,
                    "langfuse_tracing_flush_interval_seconds",
                    1.0,
                ),
            )
        ),
        "status": langfuse_status(),
    }




def _require_admin_user(user: dict | None) -> None:
    if not user or str(user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")



def _console_cookie_name(request: Request) -> str:
    # Cookie scope ignores ports. Include the public authority (from Host)
    # so two deployments on one machine do not overwrite each other's login.
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    scope = f"{request.url.hostname}:{port}/{request.scope.get('root_path', '')}"
    suffix = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    return f"{_SESSION_COOKIE}_{suffix}"


def _console_session_token(request: Request) -> str:
    # Accept an existing installation's cookie until it can be migrated after
    # successful server-side validation. Never fall back from a scoped cookie.
    return request.cookies.get(_console_cookie_name(request), request.cookies.get(_SESSION_COOKIE, ""))


def _set_console_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        _console_cookie_name(request),
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=_SESSION_TTL_SECONDS,
        path="/",
    )
    if request.cookies.get(_SESSION_COOKIE) == token:
        response.delete_cookie(_SESSION_COOKIE, path="/")


def _console_sessions_path(config) -> Path:
    """Persist console login sessions next to the users registry so a service
    restart does not force every logged-in operator back to the login page."""
    return _registry_path(config).parent / "console_sessions.json"


def _load_console_sessions(config) -> dict[str, dict]:
    path = _console_sessions_path(config)
    if getattr(config, "storage_pg_enabled", False):
        from ..storage.admin_kv import read_kv

        return read_kv(config, "console_sessions.json", path, tenant_id="default")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:  # noqa: BLE001 - corrupt/partial local file must not crash startup
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    sessions: dict[str, dict] = {}
    for token, session in data.items():
        if not isinstance(token, str) or not isinstance(session, dict):
            continue
        if float(session.get("expires_at", 0) or 0) < now:
            continue
        sessions[token] = session
    return sessions


def _save_console_sessions(config, sessions: dict[str, dict]) -> None:
    path = _console_sessions_path(config)
    if getattr(config, "storage_pg_enabled", False):
        from ..storage.admin_kv import write_kv

        write_kv(config, "console_sessions.json", path, sessions, tenant_id="default")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:  # noqa: BLE001 - persistence is best-effort, never break auth
        logger.debug("[console-auth] failed to persist console sessions", exc_info=True)


def _merge_console_sessions(target: dict[str, dict], snapshot: dict[str, dict]) -> None:
    """Merge a shared-store snapshot into the in-process session cache.

    Multi-instance deployments: each process only knows the sessions it loaded
    at startup plus the ones it issued itself. Later ``expires_at`` wins so a
    peer's sliding renewal is not undone, and expired entries are dropped so
    the cache (and the next write-back) stays bounded. Callers hold the owning
    server's ``_console_sessions_lock``.
    """
    now = time.time()
    for token, session in snapshot.items():
        if not isinstance(token, str) or not isinstance(session, dict):
            continue
        expires = float(session.get("expires_at", 0) or 0)
        if expires <= now:
            continue
        current = target.get(token)
        if not isinstance(current, dict) or float(
            current.get("expires_at", 0) or 0
        ) < expires:
            target[token] = session
    for token in [
        token
        for token, session in target.items()
        if not isinstance(session, dict)
        or float(session.get("expires_at", 0) or 0) <= now
    ]:
        target.pop(token, None)


def _refresh_console_sessions(owner, token: str) -> dict | None:
    """Re-read the shared console-session store after a cache miss.

    A session created by another instance (or another worker) is unknown to
    this process until the shared store is re-read; without this the console
    bounces between instances with ``401 login or tenant token required``.
    Returns the session when the snapshot contains a live one. The per-token
    throttle keeps a stale or forged cookie from hammering the store.
    """
    now = time.time()
    with _console_session_miss_lock:
        last = float(_console_session_misses.get(token, 0.0) or 0.0)
        if now - last < _SESSION_MISS_REFRESH_SECONDS:
            return None
        _console_session_misses[token] = now
        if len(_console_session_misses) > _SESSION_MISS_CACHE_LIMIT:
            cutoff = now - _SESSION_MISS_REFRESH_SECONDS
            for stale in [
                seen
                for seen, at in _console_session_misses.items()
                if at < cutoff
            ]:
                _console_session_misses.pop(stale, None)
    try:
        snapshot = _load_console_sessions(owner.config)
    except Exception:  # noqa: BLE001 - a store hiccup must not turn 401 into 500
        logger.warning("[console-auth] console session refresh failed", exc_info=True)
        return None
    with owner._console_sessions_lock:
        _merge_console_sessions(owner._console_sessions, snapshot)
        session = owner._console_sessions.get(token)
    return session if isinstance(session, dict) else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_session_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="session_id is required")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-/")[:160] or "session"


def _check_ingest_api_key(request: Request) -> None:
    expected = str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
    if not expected:
        return
    header = str(request.headers.get("authorization") or "").strip()
    token = header[7:].strip() if header.lower().startswith("bearer ") else header
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid ingest api key")


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    return header[7:].strip() if header.lower().startswith("bearer ") else header


def _check_v1_control_plane_key(request: Request) -> None:
    expected = str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="EVOLVE_INGEST_API_KEY is required for Agent Protocol V1 registration",
        )
    if not secrets.compare_digest(_bearer_token(request), expected):
        raise HTTPException(status_code=401, detail="invalid Agent control-plane key")


def _check_model_proxy_api_key(request: Request) -> None:
    expected = str(os.environ.get("TEAMEVOLVER_PROXY_API_KEY") or "").strip()
    if not expected:
        return
    header = str(request.headers.get("authorization") or "").strip()
    token = header[7:].strip() if header.lower().startswith("bearer ") else header
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid model proxy api key")


def _upstream_chat_url(config) -> str:
    base_url = str(getattr(config, "llm_api_base", "") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=503, detail="upstream model base URL is not configured")
    return f"{base_url}/chat/completions"


def _upstream_chat_headers(config) -> dict[str, str]:
    api_key = str(getattr(config, "llm_api_key", "") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="upstream model API key is not configured")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }


_CCR_MODEL_OVERRIDE = "deepseek-v4-flash-ga-260731"


def _model_proxy_payload(config, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    payload = dict(body)
    configured_model = str(getattr(config, "llm_model_id", "") or "").strip()
    requested_model = str(payload.get("model") or "").strip()
    lowered = requested_model.lower()
    if lowered.startswith("ccr/"):
        payload["model"] = _CCR_MODEL_OVERRIDE
    elif configured_model and requested_model in {"", "teamEvolver-model"}:
        payload["model"] = configured_model
    return payload


def _is_embedded_evolve_path(path: str) -> bool:
    if path == "/trigger-dreamcycle" or path.startswith("/trigger-dreamcycle/"):
        return False
    if path == "/validation/candidates" or path.startswith("/validation/candidates/"):
        return True
    if path.startswith("/validation/skills/"):
        return False
    if path in {"/status", "/sessions", "/conversations", "/storage/status"}:
        return False
    if path.startswith("/conversations/"):
        return False
    if path in {"/trigger", "/trigger-dreamcycle"}:
        return True
    return path.startswith(
        (
            "/storage/",
            "/validation/",
            "/skills/",
            "/trigger-dreamcycle/",
        )
    )


def _is_reusable_aggregation_path(path: str) -> bool:
    return path in {
        "/api/aggregation/users",
        "/api/aggregation/run",
    } or path.startswith("/api/aggregation/status/")


def _max_session_body_bytes() -> int:
    try:
        value = int(os.environ.get("TEAMEVOLVER_MAX_SESSION_BODY_BYTES", str(32 * 1024 * 1024)) or 0)
    except ValueError:
        value = 32 * 1024 * 1024
    return max(1024, value)


async def _read_limited_json_body(request: Request) -> dict[str, Any]:
    limit = _max_session_body_bytes()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail=f"session body exceeds {limit} bytes")
        raw.extend(chunk)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="session body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="session body must be an object")
    return parsed


def _session_queue_snapshot(config, *, limit: int = 100) -> dict[str, Any]:
    try:
        store = SessionStore.from_config(config, tenant_id=current_tenant_id())
        rows = store.list_queue(limit=limit if limit > 0 else 100000)
        return {
            "reachable": True,
            "pending": len(rows),
            "sessions": rows[:limit] if limit > 0 else [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "sessions": [], "pending": 0, "reason": str(exc)}


def _turn_skill_union(session: dict[str, Any], key: str) -> list[str]:
    """Union of a string-list skill field across turns (first-seen order)."""
    skills: list[str] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for skill in turn.get(key) or []:
            skill = str(skill).strip()
            if skill and skill not in skills:
                skills.append(skill)
    return skills


def _session_detail_payload(session: dict[str, Any]) -> dict[str, Any]:
    status = str(session.get("status") or "queued")
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    # Older Langfuse archives carry skills only on turns (the converter used to
    # leave the top-level unions empty) — fall back to the turn-level union.
    injected = session.get("injected_skills") or _turn_skill_union(session, "injected_skills")
    used = session.get("used_skills") or _turn_skill_union(session, "used_skills")
    return {
        # Conversation summary (title / submitter / status / turns). Kept
        # separate from ``meta`` so the latter keeps the same canonical
        # adapter-extracted identity shape as the ``/conversations`` list rows.
        "summary": {
            "title": session.get("title") or "",
            "user_alias": session.get("user_alias") or "",
            "status": status,
            "num_turns": len(turns) if turns else metrics.get("interaction_turns"),
        },
        # Adapter-extracted business meta (user_id / session_id / trace_id),
        # identical to the list-row shape so the detail modal shows the same
        # identity columns as 运行总览.
        "meta": session_display_meta(session),
        "turns_available": bool(turns),
        "turns_source": "archive",
        "system_prompt": session.get("system_prompt") or "",
        "injected_skills": injected,
        "used_skills": used,
        "metrics": metrics,
        "turns": turns,
        "value_judge": session.get("value_judge") if isinstance(session.get("value_judge"), dict) else {},
        # Session-level judge scores (dimensions + per-dimension reasons) so
        # the detail modal can render the full review breakdown on click.
        "judge": session.get("judge") if isinstance(session.get("judge"), dict) else {},
    }


def _history_from_archived_sessions(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    try:
        store = SessionStore.from_config(config, tenant_id=current_tenant_id())
        rows = store.list_conversations(limit=100000)
    except Exception:
        return []
    wanted = str(session_id or "").strip()
    if wanted:
        rows = [row for row in rows if str(row.get("session_id") or "") == wanted]
    cycles: list[dict[str, Any]] = []
    for row in rows[: max(0, int(limit))]:
        status = str(row.get("status") or "")
        judge = row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
        cycles.append(
            {
                "timestamp": row.get("timestamp") or row.get("ingested_at"),
                "session_ids": [row.get("session_id")],
                "sessions": 1,
                "skill_groups": 0,
                "uploaded_skills": 0,
                "candidates_queued": 0,
                "judge": {
                    "overall_score": judge.get("confidence"),
                    "rationale": judge.get("reason"),
                    "decision": judge.get("decision"),
                },
                "evolutions": [],
                "status": status,
            }
        )
    return cycles


def _build_history_bucket(config) -> Any | None:
    """Build a per-tenant object store for reading evolve history.

    Returns None when the backend would be local (the file-based fallback
    is faster and sufficient for single-tenant / local-backend deployments).
    For postgres and viking backends, the bucket enforces per-tenant RLS /
    account scoping so history records are isolated.
    """
    from team_skills.evolution.kernel.settings import EvolveServerConfig

    engine_config = EvolveServerConfig.from_teamEvolver_config(config)
    backend = str(engine_config.storage_backend or "").strip().lower()
    if backend not in ("postgres", "viking") or EvolveEngineMixin is None:
        return None
    try:
        return EvolveEngineMixin._build_bucket(engine_config)
    except Exception:  # noqa: BLE001
        return None


def _build_experience_library_store(config) -> Any:
    """Read Session lessons and legacy Evidence without starting an engine."""
    tenant_id = current_tenant_id()
    hub = SkillHub.team_from_config(config, tenant_id=tenant_id)
    sessions = SessionStore.from_config(config, tenant_id=tenant_id)
    return ExperienceLibraryStore(hub._bucket, hub._prefix(), session_store=sessions)


def _evolve_history_path_candidates(config) -> list[str]:
    raw_paths = [
        os.environ.get("EVOLVE_HISTORY_PATH", ""),
        os.environ.get("TEAMEVOLVER_EVOLVE_HISTORY_PATH", ""),
        getattr(config, "evolve_history_path", ""),
        "evolve_history.jsonl",
        os.path.join(os.getcwd(), "evolve_history.jsonl"),
    ]
    paths: list[str] = []
    for raw in raw_paths:
        value = str(raw or "").strip()
        if value and value not in paths:
            paths.append(value)
    return paths


def _cycle_matches_session(record: dict[str, Any], session_id: str) -> bool:
    wanted = str(session_id or "").strip()
    if not wanted:
        return True
    ids = set(str(item) for item in (record.get("session_ids") or []))
    for evo in record.get("evolutions") or []:
        if isinstance(evo, dict):
            ids.update(str(item) for item in (evo.get("session_ids") or []))
    return wanted in ids


def _filter_cycle_for_session(record: dict[str, Any], session_id: str) -> dict[str, Any]:
    wanted = str(session_id or "").strip()
    if not wanted:
        return dict(record)
    filtered = dict(record)
    judge = None
    for detail in record.get("session_judge_details") or []:
        if isinstance(detail, dict) and str(detail.get("session_id") or "") == wanted:
            judge = detail
            break
    filtered["judge"] = judge or record.get("judge") or {}
    filtered["evolutions"] = [
        evo
        for evo in (record.get("evolutions") or [])
        if isinstance(evo, dict) and wanted in set(str(item) for item in (evo.get("session_ids") or []))
    ]
    return filtered


_GOOD_CASE_SCORE = 0.6


def _coerce_dt(value: str, *, assume_local: bool) -> Optional[datetime]:
    """Parse an ISO timestamp/date; naive values are interpreted as UTC for
    stored row timestamps and as the server's local timezone for filter
    bounds (users pick dates in their local calendar)."""
    v = str(value or "").strip()
    if not v:
        return None
    v = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone() if assume_local else dt.replace(tzinfo=timezone.utc)
    return dt


def _row_time(row: dict[str, Any]) -> Optional[datetime]:
    return _coerce_dt(
        str(row.get("timestamp") or row.get("ingested_at") or ""),
        assume_local=False,
    )


def _filter_conversation_rows(
    rows: list[dict[str, Any]],
    *,
    search: str = "",
    status: str = "",
    decision: str = "",
    case: str = "",
    skill: str = "",
    start: str = "",
    end: str = "",
) -> list[dict[str, Any]]:
    """Server-side filters for the unified conversation list.

    ``search`` matches session_id / title / user_alias substrings; ``status``
    and ``decision`` are exact matches; ``skill`` requires the skill to be in
    the row's ``used_skills``; ``case`` filters by latest judge score
    (good >= 0.6, bad < 0.6, rows without a score are excluded); ``start`` /
    ``end`` bound the conversation time (inclusive, date-only strings mean the whole
    local day).
    """
    wanted_search = str(search or "").strip().lower()
    wanted_status = str(status or "").strip().lower()
    wanted_decision = str(decision or "").strip().lower()
    wanted_case = str(case or "").strip().lower()
    wanted_skill = str(skill or "").strip().lower()
    raw_start = str(start or "").strip()
    raw_end = str(end or "").strip()
    start_dt = _coerce_dt(raw_start, assume_local=True)
    # A date-only end bound covers the entire local day.
    end_dt = None
    end_exclusive = None
    if raw_end:
        if len(raw_end) == 10:
            parsed = _coerce_dt(raw_end, assume_local=True)
            if parsed is not None:
                end_exclusive = parsed + timedelta(days=1)
        else:
            end_dt = _coerce_dt(raw_end, assume_local=True)
    if not (
        wanted_search
        or wanted_status
        or wanted_decision
        or wanted_case
        or wanted_skill
        or start_dt
        or end_dt
        or end_exclusive
    ):
        return rows

    def _match(row: dict[str, Any]) -> bool:
        if wanted_search:
            haystack = " ".join(
                str(row.get(key) or "")
                for key in ("session_id", "title", "user_alias")
            ).lower()
            if wanted_search not in haystack:
                return False
        if wanted_status and str(row.get("status") or "").lower() != wanted_status:
            return False
        if wanted_decision:
            value_judge = row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
            if str(value_judge.get("decision") or "").lower() != wanted_decision:
                return False
        if wanted_skill:
            used = row.get("used_skills") if isinstance(row.get("used_skills"), list) else []
            if wanted_skill not in {str(s).strip().lower() for s in used}:
                return False
        if start_dt or end_dt or end_exclusive:
            ts = _row_time(row)
            if ts is None:
                return False
            if start_dt and ts < start_dt:
                return False
            if end_dt and ts > end_dt:
                return False
            if end_exclusive and ts >= end_exclusive:
                return False
        if wanted_case:
            score = (row.get("judge") or {}).get("overall_score") if isinstance(row.get("judge"), dict) else None
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return False
            if wanted_case == "good" and float(score) < _GOOD_CASE_SCORE:
                return False
            if wanted_case == "bad" and float(score) >= _GOOD_CASE_SCORE:
                return False
        return True

    return [row for row in rows if _match(row)]


def _sort_conversation_rows(
    rows: list[dict[str, Any]], sort_by: str = "", order: str = ""
) -> list[dict[str, Any]]:
    """Sort the filtered set; default stays conversation-time descending.

    Supported keys: ``time`` (timestamp, falls back to ingested_at),
    ``score`` (judge overall_score, unscored rows always last),
    ``turns`` (num_turns).
    """
    key = str(sort_by or "").strip().lower()
    if key not in ("time", "score", "turns"):
        return rows
    reverse = str(order or "desc").strip().lower() != "asc"
    if key == "score":
        # Stable two-pass sort keeps unscored rows last in both directions.
        def _score(row: dict[str, Any]) -> Optional[float]:
            raw = (row.get("judge") or {}).get("overall_score") if isinstance(row.get("judge"), dict) else None
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
            return None

        rows = sorted(rows, key=lambda r: _score(r) is None)
        rows = sorted(rows, key=lambda r: _score(r) or 0.0, reverse=reverse)
        return rows
    if key == "turns":
        return sorted(rows, key=lambda r: int(r.get("num_turns") or 0), reverse=reverse)
    return sorted(
        rows,
        key=lambda r: _row_time(r) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=reverse,
    )


def _conversation_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Quality summary over a (filtered) conversation set."""
    good = bad = valuable = chitchat = 0
    scores: list[float] = []
    for row in rows:
        raw = (row.get("judge") or {}).get("overall_score") if isinstance(row.get("judge"), dict) else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            score = float(raw)
            scores.append(score)
            if score >= _GOOD_CASE_SCORE:
                good += 1
            else:
                bad += 1
        value_judge = row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
        decision = str(value_judge.get("decision") or "").lower()
        if decision == "valuable":
            valuable += 1
        elif decision == "chitchat":
            chitchat += 1
    return {
        "total": len(rows),
        "good": good,
        "bad": bad,
        "valuable": valuable,
        "chitchat": chitchat,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
    }


_JUDGE_REASON_DIMENSIONS = (
    "task_completion",
    "response_quality",
    "efficiency",
    "tool_usage",
)


def _clean_judge_reasons(raw: Any) -> dict[str, list[str]]:
    """Normalize a judge ``reasons`` payload to ``{dimension: [bullets]}``.

    Best-effort: anything that is not a dict, or whose bullet entries are
    blank, is dropped so downstream exports stay clean.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for dim in _JUDGE_REASON_DIMENSIONS:
        items = [
            str(item).strip()
            for item in (raw.get(dim) or [])
            if str(item or "").strip()
        ]
        if items:
            cleaned[dim] = items
    return cleaned


def _session_judge_score_index(config) -> dict[str, dict[str, Any]]:
    """Latest per-session judge scores from evolve history ``session_judge_details``.

    Reads from the per-tenant object store first (RLS-isolated), falling back
    to the file-based ``evolve_history.jsonl`` for local-backend deployments.
    History records are chronological (oldest first), so the last occurrence of
    a session wins. Read-only and best-effort.
    """
    index: dict[str, dict[str, Any]] = {}

    def _process_record(record: dict[str, Any]) -> None:
        for detail in record.get("session_judge_details") or []:
            if not isinstance(detail, dict):
                continue
            sid = str(detail.get("session_id") or "").strip()
            if not sid:
                continue
            score = detail.get("overall_score")
            judged_at = str(record.get("timestamp") or "")
            prev = index.get(sid)
            if prev and prev.get("judged_at") and judged_at and prev["judged_at"] > judged_at:
                continue
            raw_reasons = detail.get("reasons")
            reasons = _clean_judge_reasons(raw_reasons)
            index[sid] = {
                "overall_score": (
                    float(score)
                    if isinstance(score, (int, float)) and not isinstance(score, bool)
                    else None
                ),
                "rationale": str(detail.get("rationale") or ""),
                "reasons": reasons,
                "judged_at": judged_at,
            }
            for dim in _JUDGE_REASON_DIMENSIONS:
                raw_dim = detail.get(dim)
                if isinstance(raw_dim, (int, float)) and not isinstance(raw_dim, bool):
                    index[sid][dim] = float(raw_dim)

    # Primary: read from the per-tenant bucket (RLS / account-scoped).
    bucket = _build_history_bucket(config)
    if bucket is not None:
        try:
            from team_skills.evolution.store.object_store import load_history_records

            for record in load_history_records(bucket):
                _process_record(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[History] bucket judge score read failed: %s", exc)

    # Also read from the legacy JSONL file(s) to merge historical records
    # that predate the bucket-based write path.
    for path in _evolve_history_path_candidates(config):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(record, dict):
                        continue
                    _process_record(record)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - ledger enrichment is best-effort
            logger.warning("[History] failed to read judge scores from %s: %s", path, exc)
            continue
    return index


def _history_from_evolve_file(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    capped = max(1, int(limit or 50))

    # Merge bucket records (per-tenant isolated) with legacy file records
    # so historical data isn't lost after upgrading to the bucket path.
    bucket_records: list[dict[str, Any]] = []
    bucket = _build_history_bucket(config)
    if bucket is not None:
        try:
            from team_skills.evolution.store.object_store import load_history_records

            bucket_records = load_history_records(bucket, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[History] bucket history read failed: %s", exc)

    file_records: list[dict[str, Any]] = []
    # The legacy evolve_history.jsonl is a process-global single-tenant file
    # (CWD-relative) belonging to the default tenant. A switched tenant is
    # isolated by its bucket (PG RLS / per-tenant Viking account), so never
    # merge the global file into its evolution audit view.
    if current_tenant_id() == DEFAULT_TENANT_ID:
        for path in _evolve_history_path_candidates(config):
            rows: list[dict[str, Any]] = []
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(record, dict) or not _cycle_matches_session(record, session_id):
                            continue
                        rows.append(record)
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("[History] failed to read evolve history %s: %s", path, exc)
                continue
            file_records.extend(rows)

    # Dedup by timestamp + cycle_id, bucket wins on conflict.
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for record in bucket_records + file_records:
        key = str(record.get("timestamp") or "") + str(record.get("cycle_id") or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    merged.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return [_filter_cycle_for_session(r, session_id) for r in merged[:capped]]


def _history_cycles(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    cycles = _history_from_evolve_file(config, limit=limit, session_id=session_id)
    if cycles:
        return cycles
    return _history_from_archived_sessions(config, limit=limit, session_id=session_id)


def _candidate_skill_name(job: dict[str, Any]) -> str:
    candidate_skill = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else {}
    return str(
        job.get("skill_name")
        or job.get("candidate_skill_name")
        or candidate_skill.get("name")
        or ""
    )


def _scrub_legacy_reward_text(text: Any) -> str:
    value = str(text or "")
    legacy_marker = "P" + "RM"
    value = re.sub(rf"\s*\({legacy_marker}\s+[-+]?\d+(?:\.\d+)?\)", "", value, flags=re.IGNORECASE)
    value = re.sub(rf"\b{legacy_marker}\b", "session quality score", value, flags=re.IGNORECASE)
    return value


def _candidate_payload(
    job: dict[str, Any],
    evaluation: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(job)
    name = _candidate_skill_name(payload)
    if name:
        payload["skill_name"] = name
        payload.setdefault("candidate_skill_name", name)
    payload["proposed_action"] = str(payload.get("proposed_action") or payload.get("action") or "")
    if payload.get("rationale"):
        payload["rationale"] = _scrub_legacy_reward_text(payload.get("rationale"))
    if decision:
        status = str(decision.get("status") or "").strip()
        if not status:
            status = "published" if decision.get("accepted") is True else "rejected"
        payload["review_status"] = status
        payload["decision"] = {
            key: decision.get(key)
            for key in (
                "status",
                "accepted",
                "reason",
                "decided_at",
                "job_id",
                "skill_name",
                "version",
                "mode",
                "reviewer",
            )
            if decision.get(key) is not None
        }
        payload["decision_reason"] = str(decision.get("reason") or "")
        payload["decided_at"] = str(decision.get("decided_at") or decision.get("created_at") or "")
        payload["decision_accepted"] = decision.get("accepted")
        if evaluation is None and isinstance(decision.get("evaluation"), dict):
            evaluation = decision.get("evaluation")
    else:
        payload["review_status"] = "open"
    test_datasets = [
        item
        for item in job.get("test_datasets") or []
        if isinstance(item, dict)
    ]
    payload["test_dataset_count"] = len(test_datasets)
    payload["test_dataset_ids"] = [
        str(item.get("dataset_id") or "") for item in test_datasets
    ]
    if evaluation:
        eval_payload = _evaluation_payload(job, evaluation, cached=True)
        replay_payload = eval_payload.get("replay") if isinstance(eval_payload.get("replay"), dict) else {}
        payload["evaluation"] = eval_payload
        payload["recommended_publish"] = eval_payload.get("recommended_publish")
        payload["evaluation_error"] = replay_payload.get("error")
        payload["replay_verdict"] = replay_payload.get("verdict")
        payload["efficiency"] = replay_payload.get("efficiency") or {}
    for key in (
        "min_score",
        "checklist",
        "inherited_checklists",
        "evolution_context",
        "session_evidence",
        "replay_cases",
        "test_datasets",
        "verification",
        "verify_score",
        "replay_score",
        "baseline_score",
        "threshold",
    ):
        payload.pop(key, None)
    return payload


def _candidate_list_payloads(
    store: ValidationStore,
    *,
    scope: str = "open",
    user_alias: str = "",
) -> list[dict[str, Any]]:
    normalized = str(scope or "open").strip().lower()
    if normalized in {"history", "processed", "closed", "decided"}:
        normalized = "processed"
    elif normalized in {"all", "any"}:
        normalized = "all"
    else:
        normalized = "open"

    indexed_records = (
        store.list_decision_records(reconcile=False)
        if normalized in {"processed", "all"}
        else []
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in indexed_records:
        job = (
            record.get("job")
            if isinstance(record.get("job"), dict)
            else {}
        )
        job_id = str(job.get("job_id") or record.get("job_id") or "")
        if not job_id:
            continue
        evaluation = (
            record.get("evaluation")
            if isinstance(record.get("evaluation"), dict)
            else None
        )
        decision = (
            record.get("decision")
            if isinstance(record.get("decision"), dict)
            else None
        )
        candidates.append(_candidate_payload(job, evaluation, decision))
        seen.add(job_id)
    if normalized == "processed":
        return candidates
    for job in store.list_open_jobs():
        job_id = str(job.get("job_id") or "")
        if not job_id or job_id in seen:
            continue
        evaluation = store.load_best_evaluation(job_id, job) if job_id else None
        candidates.append(_candidate_payload(job, evaluation, None))
    return candidates


def _compact_candidate_payload(item: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: value
        for key, value in item.items()
        if key
        not in {
            "candidate_skill",
            "current_skill",
            "candidate_skill_md",
            "current_skill_md",
            "skill_diff",
        }
    }
    evaluation = compact.get("evaluation")
    if isinstance(evaluation, dict):
        evaluation = dict(evaluation)
        for key in (
            "candidate_skill",
            "current_skill",
            "candidate_skill_md",
            "current_skill_md",
            "skill_diff",
        ):
            evaluation.pop(key, None)
        replay = evaluation.get("replay")
        if isinstance(replay, dict):
            replay = dict(replay)
            replay["cases"] = []
            evaluation["replay"] = replay
        compact["evaluation"] = evaluation
    return compact




def _aggregate_window_dimensions(windows: Any) -> dict[str, Any]:
    """Sum per-window efficiency dimensions into a flat ``dimensions`` block.

    ``windows`` may be ``efficiency.windows`` or the summary's
    ``window_results`` — both carry ``<window>.dimensions`` (older
    aggregators) or ``<window>.efficiency.dimensions`` (window_results).
    """
    if not isinstance(windows, dict) or not windows:
        return {}
    from team_replay.metrics import UNAVAILABLE, compare_efficiency, metric_number

    metric_keys = ("interaction_turns", "tool_call_count", "total_tokens")
    totals: dict[str, dict[str, int]] = {
        key: {"baseline": 0, "candidate": 0} for key in metric_keys
    }
    found = False
    unavailable: set[tuple[str, str]] = set()
    for window in windows.values():
        if not isinstance(window, dict):
            continue
        dims = window.get("dimensions")
        if not isinstance(dims, dict):
            nested = window.get("efficiency") if isinstance(window.get("efficiency"), dict) else {}
            dims = nested.get("dimensions") if isinstance(nested.get("dimensions"), dict) else None
        if not isinstance(dims, dict):
            continue
        for key in metric_keys:
            metric = dims.get(key) if isinstance(dims.get(key), dict) else {}
            for branch in ("baseline", "candidate"):
                value = metric_number(metric.get(branch))
                if value is None:
                    unavailable.add((key, branch))
                else:
                    totals[key][branch] += value
            found = True
    if not found:
        return {}
    branches = {
        branch: {
            key: UNAVAILABLE if (key, branch) in unavailable else totals[key][branch] for key in metric_keys
        }
        for branch in ("baseline", "candidate")
    }
    dimensions = compare_efficiency(branches["baseline"], branches["candidate"])["dimensions"]
    return {key: dimensions[key] for key in metric_keys}


def _normalize_efficiency(replay_summary: dict[str, Any]) -> dict[str, Any]:
    """Return an efficiency block with a top-level ``dimensions`` when possible.

    Efficiency data was persisted in three historical shapes:
      * flat ``efficiency.dimensions`` (dry-run evaluations),
      * ``efficiency.windows.{recent,historical}.dimensions`` (newer worker),
      * only ``replay_summary.window_results.<window>.efficiency.dimensions``
        (older worker that omitted the top-level ``efficiency`` summary).

    The dashboard reads only the flat shape, so recover it from whichever
    shape is available. Returns ``{}`` when no efficiency data was captured.
    """
    if not isinstance(replay_summary, dict):
        return {}
    efficiency = replay_summary.get("efficiency") if isinstance(replay_summary.get("efficiency"), dict) else {}
    if isinstance(efficiency.get("dimensions"), dict) and efficiency.get("dimensions"):
        dimensions = efficiency.get("dimensions") or {}
        return {
            "baseline": efficiency.get("baseline") or {},
            "candidate": efficiency.get("candidate") or {},
            "dimensions": {
                key: {
                    field: value.get(field)
                    for field in (
                        "baseline",
                        "candidate",
                        "delta",
                        "reduction_ratio",
                        "winner",
                    )
                }
                for key, value in dimensions.items()
                if isinstance(value, dict)
            },
        }
    dimensions = _aggregate_window_dimensions(efficiency.get("windows"))
    if not dimensions:
        dimensions = _aggregate_window_dimensions(replay_summary.get("window_results"))
    if not dimensions:
        return efficiency
    return {
        "baseline": efficiency.get("baseline") or {},
        "candidate": efficiency.get("candidate") or {},
        "dimensions": dimensions,
    }


def _evaluation_payload(job: dict[str, Any], result: dict[str, Any], *, cached: bool = False) -> dict[str, Any]:
    replay_summary = result.get("replay_summary") if isinstance(result.get("replay_summary"), dict) else {}
    if not replay_summary and isinstance(result.get("replay"), dict):
        replay_summary = result.get("replay") or {}
    cases = replay_summary.get("cases") if isinstance(replay_summary.get("cases"), list) else []
    normalized_cases: list[dict[str, Any]] = []
    fallback_reason = result.get("true_replay_fallback_reason")

    def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in mapping and mapping.get(key) is not None:
                return mapping.get(key)
        return None

    def _branch_payload(branch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        response = _first_present(branch, "final_response", "response_text", "response") or ""
        if not response and isinstance(branch.get("interactions"), list):
            for interaction in reversed(branch.get("interactions") or []):
                if isinstance(interaction, dict) and interaction.get("response"):
                    response = interaction.get("response") or ""
                    break
        error = str(branch.get("error") or "")
        rationale = str(branch.get("rationale") or branch.get("replay_reason") or "")
        display_response = response or error or rationale
        return {
            "response": display_response,
            "error": error,
            "rationale": rationale,
            "instruction": branch.get("instruction") or item.get("instruction") or "",
            "session_id": branch.get("session_id") or item.get("session_id") or "",
            "turn_num": branch.get("turn_num") if branch.get("turn_num") is not None else item.get("turn_num"),
            "interaction_turns": branch.get("interaction_turns"),
            "tool_call_count": branch.get("tool_call_count"),
            "total_tokens": branch.get("total_tokens"),
            "interactions": branch.get("interactions") or [],
            "checklist_report": branch.get("checklist_report") or {},
        }

    for item in cases:
        if not isinstance(item, dict):
            continue
        if "baseline" in item or "candidate" in item:
            baseline = item.get("baseline") if isinstance(item.get("baseline"), dict) else {}
            candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
            normalized_cases.append(
                {
                    "baseline": _branch_payload(baseline, item),
                    "candidate": _branch_payload(candidate, item),
                }
            )
    skill_name = _candidate_skill_name(job)
    efficiency = _normalize_efficiency(replay_summary)
    branch_checklists = (
        replay_summary.get("checklist")
        if isinstance(replay_summary.get("checklist"), dict)
        else {
            branch: aggregate_case_checklists(
                normalized_cases,
                branch=branch,
            )
            for branch in ("baseline", "candidate")
        }
    )
    policy = progressive_replay_decision(
        efficiency=efficiency,
        baseline_checklist=branch_checklists.get("baseline") or {},
        candidate_checklist=branch_checklists.get("candidate") or {},
    )
    accepted = bool(policy.get("accepted"))
    no_regression = bool(policy.get("no_regression"))
    verdict = str(policy.get("verdict") or "inconclusive")
    return {
        "status": "evaluated",
        "skill_name": skill_name,
        "proposed_action": str(job.get("proposed_action") or job.get("action") or ""),
        "recommended_publish": bool(accepted),
        "cached": cached,
        "replay": {
            "verdict": verdict,
            "no_regression": bool(no_regression),
            "cases": normalized_cases,
            "efficiency": efficiency,
            "checklist": branch_checklists,
            "decision_policy": policy,
            "mode": result.get("validator_mode"),
            "error": fallback_reason or replay_summary.get("error"),
        },
        "candidate_skill": job.get("candidate_skill"),
        "current_skill": job.get("current_skill"),
    }


async def _evaluate_candidate_job(config, owner, job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    try:
        from team_replay.contracts import skill_replay_spec
        from team_replay.engine import evaluate_job
        from team_replay.gateway import EmbeddedReplayGateway
        from teamEvolver.replay_adapter import ensure_replay_host

        ensure_replay_host()
        try:
            replay_timeout = max(10, int(os.environ.get("TEAMEVOLVER_TRUE_REPLAY_TIMEOUT_S", "90")))
        except ValueError:
            replay_timeout = 90
        try:
            max_interactions = max(
                1,
                int(
                    os.environ.get(
                        "TEAMEVOLVER_TRUE_REPLAY_MAX_INTERACTIONS",
                        str(job.get("max_interactions") or 4),
                    )
                ),
            )
        except ValueError:
            max_interactions = max(1, int(job.get("max_interactions") or 4))
        replay_gateway = EmbeddedReplayGateway(evaluate_job)
        replay_spec = skill_replay_spec(
            job,
            account_id=str(
                getattr(config, "viking_account", "")
                or getattr(config, "sharing_viking_account", "")
                or "default"
            ),
        )
        selected = select_replay_cases(job.get("replay_cases") or [])
        window_results = []
        for window, case_index in selected:
            result = await asyncio.to_thread(
                replay_gateway.evaluate,
                replay_spec,
                case_index=case_index,
                timeout_seconds=replay_timeout,
                max_interactions=max_interactions,
            )
            window_results.append((window, result))
        replay = aggregate_true_replay_windows(
            window_results,
        )
        if replay.get("status") == "evaluated":
            replay_decision = str(
                replay.get("verdict")
                or (
                    "accept"
                    if replay.get("accepted")
                    else "inconclusive"
                )
            )
            return {
                "validator_mode": "true_replay",
                "decision": replay_decision,
                "accepted": bool(replay.get("accepted")),
                "reason": replay_decision,
                "replay_summary": replay,
            }
        logger.info("[Validation] true replay skipped for %s: %s", job_id, replay.get("reason"))
        fallback_reason = replay.get("reason") or replay.get("status") or "true replay skipped"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Validation] true replay failed for %s: %s", job_id, exc)
        fallback_reason = f"true replay failed: {type(exc).__name__}: {exc}"

    return {
        "validator_mode": "true_replay",
        "decision": "inconclusive",
        "accepted": False,
        "reason": fallback_reason,
        "replay_summary": {
            "status": "skipped",
            "reason": fallback_reason,
            "cases": [],
        },
    }


def _load_current_skill_md_for_display(config, skill_name: str) -> str:
    """Fetch the published baseline SKILL.md for candidate diff display.

    Always reads from the configured durable team Skill store (local/NAS or
    OpenViking), which is the same store the evolve server publishes to.
    Working Skill directories are deliberately not consulted because they can
    drift from the published baseline.
    """
    name = str(skill_name or "").strip()
    if not name:
        return ""
    try:
        hub = SkillHub.team_from_config(config, tenant_id=current_tenant_id())
        for record in hub.list_remote():
            if str(record.get("name") or "") != name:
                continue
            bundle = hub._download_skill_bundle(name, record)
            return bundle.get("SKILL.md", b"").decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _skill_diff_payload(job: dict[str, Any], config=None) -> dict[str, Any]:
    import difflib

    current = job.get("current_skill") if isinstance(job.get("current_skill"), dict) else None
    candidate = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else None
    current_md = build_skill_md(current).splitlines() if current else []
    candidate_md = build_skill_md(candidate).splitlines() if candidate else []
    if not current_md and config is not None:
        current_raw = _load_current_skill_md_for_display(config, _candidate_skill_name(job))
        if current_raw:
            current_md = current_raw.splitlines()
    diff = "\n".join(
        difflib.unified_diff(
            current_md,
            candidate_md,
            fromfile="current/SKILL.md",
            tofile="candidate/SKILL.md",
            lineterm="",
        )
    )
    return {
        "current_skill_md": "\n".join(current_md),
        "candidate_skill_md": "\n".join(candidate_md),
        "skill_diff": diff,
    }


def _configured_storage_backend(config, field: str) -> str:
    return normalize_backend(str(getattr(config, field, "") or "")) or "local"


def _skill_set_generation(values: dict[str, str]) -> str:
    encoded = json.dumps(
        sorted(values.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _skill_cache_status(owner, hub: SkillHub) -> dict[str, Any]:
    manifest = {
        str(item.get("name") or ""): dict(item)
        for item in hub.list_remote()
        if str(item.get("name") or "")
    }
    try:
        manifest_raw = hub._bucket.get_object(hub._manifest_key()).read()
    except Exception as exc:  # noqa: BLE001
        if not is_not_found_error(exc):
            raise
        manifest_raw = b""

    expected: dict[str, str] = {}
    for name, record in manifest.items():
        expected[name] = str(
            record.get("tree_sha256") or record.get("sha256") or ""
        )

    skills_dir = owner._skills_dir()
    local: dict[str, str] = {}
    for name, paths in hub._list_local_skill_dirs(skills_dir).items():
        if not paths:
            continue
        bundle = read_skill_bundle(paths[-1])
        record = manifest.get(name) or {}
        if record.get("tree_sha256"):
            local[name] = bundle_tree_sha256(bundle)
            continue
        try:
            local[name] = hashlib.sha256(
                bundle_entrypoint_bytes(bundle)
            ).hexdigest()
        except Exception:
            local[name] = ""

    missing = sorted(set(expected) - set(local))
    extra = sorted(set(local) - set(expected))
    mismatched = sorted(
        name
        for name in set(expected) & set(local)
        if expected[name] != local[name]
    )
    return {
        "local_cache_skills": len(local),
        "local_cache_generation": _skill_set_generation(local),
        "manifest_skills": len(manifest),
        "manifest_generation": _skill_set_generation(expected),
        "manifest_hash": hashlib.sha256(manifest_raw).hexdigest(),
        "local_cache_matches_manifest": not (missing or extra or mismatched),
        "local_cache_missing": missing,
        "local_cache_extra": extra,
        "local_cache_mismatched": mismatched,
    }


def _storage_status(config, owner=None) -> dict[str, Any]:
    configured_skill_backend = _configured_storage_backend(
        config,
        "sharing_skill_backend",
    )
    configured_session_backend = _configured_storage_backend(
        config,
        "sharing_session_backend",
    )
    tenant_id = current_tenant_id()
    endpoint = str(getattr(config, "sharing_viking_endpoint", "") or getattr(config, "sharing_endpoint", "") or "")
    deployment = str(getattr(config, "sharing_viking_deployment", "") or "cloud")
    namespace = "resources" if configured_skill_backend == "viking" else configured_skill_backend
    api_key_present = bool(
        str(getattr(config, "sharing_viking_team_api_key", "") or "")
        or str(getattr(config, "sharing_viking_api_key", "") or "")
    )
    payload: dict[str, Any] = {
        "backend": configured_skill_backend,
        "deployment": deployment,
        "endpoint": endpoint,
        "namespace": namespace,
        "api_key_present": api_key_present,
        "sharing_enabled": bool(getattr(config, "sharing_enabled", False)),
        "fallback_enabled": bool(getattr(config, "sharing_local_fallback_enabled", True)),
        "effective_backend": configured_skill_backend,
        "fallback_active": False,
        "reachable": False,
        "instance_id": _INSTANCE_ID,
        "tenant_id": tenant_id,
        "skill_backend": configured_skill_backend,
        "session_backend": configured_session_backend,
        "multi_replica_safe": False,
    }
    try:
        hub = SkillHub.team_from_config(config, tenant_id=tenant_id)
        skill_source = hub.describe_source()
        # Probe the configured store. Missing manifest is still a successful
        # connectivity check: it means the bucket/key is reachable but empty.
        try:
            hub._bucket.get_object(hub._manifest_key())
        except Exception as exc:  # noqa: BLE001
            if not is_not_found_error(exc):
                raise
        payload["reachable"] = True
        skill_effective_backend = str(skill_source.get("backend") or "")
        payload["effective_backend"] = skill_effective_backend
        payload["fallback_active"] = bool(skill_source.get("fallback"))
        payload["skill_storage"] = {
            "configured_backend": configured_skill_backend,
            "effective_backend": skill_effective_backend,
            "reachable": True,
            "fallback_active": bool(skill_source.get("fallback")),
        }
        if isinstance(hub._bucket, LocalObjectStore):
            payload["local_root"] = getattr(hub._bucket, "root", "")
            payload["skill_storage"]["local_root"] = payload["local_root"]
        if skill_source.get("reason"):
            payload["skill_storage"]["reason"] = str(skill_source["reason"])

        # Per-purpose split status + mirror outbox backlog (when the team
        # skill library is local-backed with mirroring enabled).
        session_hub = SkillHub.object_storage_from_config(
            config,
            tenant_id=tenant_id,
        )
        session_effective_backend = "none"
        session_reachable = False
        if session_hub is not None:
            session_source = session_hub.describe_source()
            session_effective_backend = str(session_source.get("backend") or "")
            try:
                session_hub._bucket.get_object("__storage_status_probe__")
            except Exception as exc:  # noqa: BLE001
                if not is_not_found_error(exc):
                    raise
            session_reachable = True
            payload["session_storage"] = {
                "configured_backend": configured_session_backend,
                "effective_backend": session_effective_backend,
                "reachable": True,
                "fallback_active": bool(session_source.get("fallback")),
            }
        else:
            payload["session_storage"] = {
                "configured_backend": configured_session_backend,
                "effective_backend": "none",
                "reachable": False,
                "fallback_active": False,
            }

        pg_bucket = next(
            (
                candidate
                for candidate in (
                    hub._bucket,
                    getattr(session_hub, "_bucket", None),
                )
                if isinstance(candidate, PgObjectStore)
            ),
            None,
        )
        if pg_bucket is not None:
            payload["pg"] = pg_bucket.pool_status()
        payload["multi_replica_safe"] = (
            skill_effective_backend == "postgres"
            and session_effective_backend == "postgres"
            and session_reachable
        )

        try:
            payload["outbox"] = SkillMutationService.from_hub(
                hub,
                config=config,
            ).health()
        except Exception as exc:  # noqa: BLE001
            payload["outbox"] = {"error": str(exc)}

        from ..integrations.skillopt_rollout import supervisor_status

        rollout = supervisor_status()
        payload["rollout_supervisor"] = {
            "running": bool(rollout.get("running")),
            "updated_at": str(rollout.get("updated_at") or ""),
            "tenant": dict(
                (rollout.get("tenants") or {}).get(tenant_id) or {}
            ),
        }
        if owner is not None:
            try:
                payload["skill_cache"] = _skill_cache_status(owner, hub)
            except Exception as exc:  # noqa: BLE001
                payload["skill_cache"] = {"error": str(exc)}

        mirror_hub = getattr(hub, "mirror_viking_hub", None)
        payload["mirror_enabled"] = mirror_hub is not None
        if mirror_hub is not None:
            try:
                from team_skills.library.mirror import VikingSkillMirror

                spool_dir = str(getattr(config, "sharing_skill_mirror_spool_dir", "") or "") or None
                payload["mirror"] = VikingSkillMirror(
                    spool_dir=spool_dir,
                    viking_hub=mirror_hub,
                    sequence_bucket=getattr(hub, "_bucket", None),
                ).status()
            except Exception as exc:  # noqa: BLE001 - status must never raise
                payload["mirror"] = {"enabled": True, "error": str(exc)}
        return payload
    except Exception as exc:  # noqa: BLE001
        payload["reason"] = str(exc)
        return payload


class RoutesMixin:
    """FastAPI app construction, routing, and request authentication."""

    def _build_app(self) -> FastAPI:
        owner = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            if bool(getattr(owner.config, "storage_pg_enabled", False)):
                registry = get_tenant_registry(owner)
                status = await asyncio.to_thread(
                    registry.runtime.pool_status,
                    timeout=max(
                        30.0,
                        float(getattr(owner.config, "storage_pg_command_timeout_seconds", 30.0)),
                    ),
                )
                if not status.get("reachable"):
                    raise RuntimeError(f"PostgreSQL startup check failed: {status.get('reason')}")
                # Pre-warm the main data-plane pool so the first /conversations
                # request doesn't pay the ~12s asyncpg pool bootstrap cost
                # (SSL handshake + schema DDL) on a remote PG instance.
                try:
                    from ..storage.pg_store import PgObjectStore
                    dsn = str(getattr(owner.config, "storage_pg_dsn", "") or "")
                    if not dsn:
                        from ..storage.pg_pool import dsn_from_env
                        dsn = dsn_from_env()
                    if dsn:
                        warm_store = PgObjectStore(
                            dsn=dsn,
                            schema=str(getattr(owner.config, "storage_pg_schema", "teamevolver")),
                            tenant_id="default",
                            pool_min=int(getattr(owner.config, "storage_pg_pool_min", 2)),
                            pool_max=int(getattr(owner.config, "storage_pg_pool_max", 20)),
                            command_timeout=float(getattr(owner.config, "storage_pg_command_timeout_seconds", 30.0)),
                            ssl=str(getattr(owner.config, "storage_pg_ssl", "prefer")),
                        )
                        warm_store.pool_status(timeout=30.0)
                        logger.info("[Startup] main PG pool pre-warmed")
                except Exception:  # noqa: BLE001 - pre-warm is best-effort
                    logger.warning("[Startup] main pool pre-warm failed", exc_info=True)
            owner._start_skill_reload_polling()
            owner._start_embedded_evolve()
            datasource_runtime = getattr(owner, "_datasource_pull_runtime", None)
            if datasource_runtime is not None:
                datasource_runtime.start()
            judge_queue = getattr(owner, "_session_judge_queue", None)
            if judge_queue is not None:
                try:
                    judge_queue.start()
                except Exception:  # noqa: BLE001 - post-ingest judging is best-effort
                    logger.warning("[SessionJudge] queue start failed", exc_info=True)
            # DreamCycle is superseded by the ov compile-based cross-user memory
            # aggregation in team_memory. It no longer auto-starts;
            # team memory is now maintained under viking://resources/shared-knowledge/.
            if os.environ.get("TEAMEVOLVER_SKILLMINER_ENABLED", "1") == "1":
                try:
                    await asyncio.to_thread(owner._start_skillminer)
                except Exception:
                    logger.debug("[SkillMiner] eager start failed", exc_info=True)
            owner._ready_event.set()
            try:
                yield
            finally:
                owner._ready_event.clear()
                await owner._shutdown_cleanup()

        app = FastAPI(title="teamEvolver", lifespan=lifespan)
        app.state.owner = self
        self._console_sessions = getattr(self, "_console_sessions", None)
        if self._console_sessions is None:
            self._console_sessions = _load_console_sessions(self.config)
        self._console_sessions_lock = threading.RLock()
        dist_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "dist"))
        dist_index = os.path.join(dist_dir, "index.html")
        dist_assets = os.path.join(dist_dir, "assets")
        if os.path.isdir(dist_assets):
            app.mount("/assets", StaticFiles(directory=dist_assets), name="assets")
        docs_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "assets"))
        if os.path.isdir(docs_assets):
            app.mount("/docs-assets", StaticFiles(directory=docs_assets), name="docs-assets")

        def _issue_console_session(user_id: str) -> str:
            token = secrets.token_urlsafe(32)
            now = time.time()
            with owner._console_sessions_lock:
                owner._console_sessions[token] = {
                    "user_id": user_id,
                    "created_at": now,
                    "expires_at": now + _SESSION_TTL_SECONDS,
                }
                _save_console_sessions(owner.config, owner._console_sessions)
            return token

        def _revoke_console_session(token: str) -> bool:
            with owner._console_sessions_lock:
                if owner._console_sessions.pop(token, None) is not None:
                    _save_console_sessions(owner.config, owner._console_sessions)
                    return True
            return False

        def _session_user(request: Request) -> dict | None:
            token = _console_session_token(request)
            if not token:
                return None
            with owner._console_sessions_lock:
                session = owner._console_sessions.get(token)
            if not isinstance(session, dict):
                # Cache miss: the session may have been issued by another
                # instance/worker, so re-read the shared store once before
                # rejecting the caller (otherwise the console 401s whenever a
                # request lands on a peer).
                session = _refresh_console_sessions(owner, token)
                if not isinstance(session, dict):
                    return None
            with owner._console_sessions_lock:
                if float(session.get("expires_at", 0) or 0) <= time.time():
                    _revoke_console_session(token)
                    return None
            user_id = str(session.get("user_id") or "")
            if not user_id:
                return None
            data = _load_registry(_registry_path(owner.config), owner.config)
            try:
                _idx, user = _find_user(data, user_id)
            except HTTPException:
                _revoke_console_session(token)
                return None
            with owner._console_sessions_lock:
                if owner._console_sessions.get(token) is not session:
                    return None  # A concurrent logout must not resurrect it.
                now = time.time()
                last_renewed = float(session.get("expires_at", 0)) - _SESSION_TTL_SECONDS
                legacy_cookie = _console_cookie_name(request) not in request.cookies
                if legacy_cookie or now - last_renewed >= _SESSION_RENEW_INTERVAL_SECONDS:
                    session["expires_at"] = now + _SESSION_TTL_SECONDS
                    _save_console_sessions(owner.config, owner._console_sessions)
                    request.state.renew_console_session = token
            return _public_user(user, owner.config)

        def _users_empty() -> bool:
            data = _load_registry(_registry_path(owner.config), owner.config)
            return not bool(data.get("users"))

        @app.middleware("http")
        async def embedded_evolve_routes(request: Request, call_next):
            if _is_embedded_evolve_path(request.url.path):
                response = await owner._dispatch_embedded_evolve_request(request)
                if response is not None:
                    return response
            return await call_next(request)

        @app.middleware("http")
        async def tenant_context(request: Request, call_next):
            """Resolve the request's tenant (multi-tenancy plan Phase 1).

            ``tevt_`` machine credentials map to tenants server-side — in
            multi-tenant deployments via the tenant's issued credential, in
            single-tenant deployments via the operator-configured default
            credential. Console admins may switch via ``X-Tenant-Id``;
            everything else stays on the implicit default tenant.
            Registered between the embedded-dispatch and console-auth
            middlewares, so ``request.state.console_user`` is already set here.
            """
            registry = get_tenant_registry(owner)
            path = request.url.path
            ctx = None
            source = "default"
            try:
                if not path.startswith("/v1/"):
                    token = _bearer_token(request)
                    claimed = str(request.headers.get("x-tenant-id") or "").strip()
                    if token.startswith(AGENT_TOKEN_PREFIX):
                        ctx = await asyncio.to_thread(registry.resolve_by_agent_token, token)
                        if ctx is None or ctx.status != "active":
                            return JSONResponse(status_code=401, content={"detail": "invalid tenant token"})
                        machine_paths = (
                            "/ingest_session", "/langfuse/pull", "/api/datasource/pull", "/trigger", "/status",
                            "/history", "/sessions", "/conversations", "/storage/status",
                            # Context uses the caller-declared user within the token's tenant.
                            # Keep this entry scoped to /context — never widen it to
                            # /internal/agents (that would expose /internal/agents/register).
                            "/internal/agents/context",
                        )
                        if path != "/sync/skills" and not any(
                            path == prefix or path.startswith(prefix + "/") for prefix in machine_paths
                        ):
                            return JSONResponse(status_code=403, content={"detail": "console admin required"})
                        if ctx is not None:
                            source = "token"
                            if claimed and claimed != ctx.tenant_id:
                                # The tenant is derived from the token; a
                                # client-supplied override never wins.
                                return JSONResponse(
                                    status_code=403, content={"detail": "tenant mismatch"}
                                )
                    elif (
                        claimed
                        and registry.mode == "postgres"
                        and not path.startswith("/v1/")
                    ):
                        # Accept the console-admin tenant selector on every
                        # non-/v1/ endpoint (dashboard endpoints such as
                        # /status, /conversations, /storage/status live
                        # outside /api/ and must be tenant-scoped too).
                        user = getattr(request.state, "console_user", None)
                        if user is None:
                            return JSONResponse(
                                status_code=401, content={"detail": "login required"}
                            )
                        if str(user.get("role") or "user") != "admin":
                            return JSONResponse(
                                status_code=403,
                                content={"detail": "admin required for tenant switch"},
                            )
                        target = await asyncio.to_thread(registry.get, claimed)
                        if target is None or target.status != "active":
                            return JSONResponse(
                                status_code=403,
                                content={"detail": f"unknown tenant: {claimed}"},
                            )
                        ctx = target
                        source = "console"
                if ctx is None:
                    ctx = registry.default_context()
                request.state.tenant = ctx
                request.state.tenant_id = ctx.tenant_id
                request.state.tenant_source = source
                tenant_token = set_current_tenant(ctx)
                try:
                    from ..logging_runtime import log_context
                    with log_context(tenant=ctx.tenant_id,
                                     user=(getattr(request.state, "console_user", None) or {}).get("id", "")):
                        response = await call_next(request)
                    if getattr(request.state, "agent_legacy_identity", False):
                        response.headers["Deprecation"] = "@1789603200"
                        response.headers["Sunset"] = "Thu, 17 Dec 2026 00:00:00 GMT"
                        response.headers["Link"] = '</docs/agent-integration-protocol-v2.md>; rel="successor-version"'
                    return response
                finally:
                    reset_current_tenant(tenant_token)
            except HTTPException:
                raise
            except Exception:
                logger.exception("[Tenants] tenant resolution failed")
                raise

        @app.middleware("http")
        async def require_console_auth(request: Request, call_next):
            path = request.url.path
            import hmac

            root_key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")
            bearer = _bearer_token(request)
            if root_key and bearer and hmac.compare_digest(bearer, root_key):
                request.state.service_root_authenticated = True
                request.state.console_user = {"id": "admin", "role": "admin"}
                return await call_next(request)
            if bearer.startswith(RETIRED_AGENT_TOKEN_PREFIX):
                # Per-Agent access tokens are retired: agents must present the
                # tenant machine credential. Answer with an actionable reason so
                # credentials cached in agent configs are diagnosable.
                return JSONResponse(
                    status_code=401,
                    content={"detail": "AGENT_ACCESS_TOKEN_RETIRED"},
                )
            if bool(getattr(owner.config, "storage_pg_enabled", False)):
                if path.startswith("/v1/"):
                    return JSONResponse(status_code=401, content={"detail": "service root key required"})
                public = path in {"/", "/console", "/health", "/healthz", "/readyz", "/favicon.ico"} or path.startswith(
                    ("/assets/", "/docs-assets/", "/api/auth/")
                )
                if not public and not path.startswith("/v1/"):
                    user = await asyncio.to_thread(_session_user, request)
                    if user is not None:
                        request.state.console_user = user
                    elif not bearer.startswith(AGENT_TOKEN_PREFIX):
                        return JSONResponse(status_code=401, content={"detail": "login or tenant token required"})
                    # Tenant tokens are authenticated by the inner middleware;
                    # admin-only handlers still require an admin console user.
                    return await call_next(request)
            reusable_aggregation = _is_reusable_aggregation_path(path)
            requires_auth = (
                path.startswith("/api/")
                and not path.startswith("/api/auth/")
                and not reusable_aggregation
            )
            if reusable_aggregation:
                user = await asyncio.to_thread(_session_user, request)
                if user is not None:
                    request.state.console_user = user
            elif requires_auth:
                if await asyncio.to_thread(_users_empty):
                    return JSONResponse(status_code=401, content={"detail": "setup required", "needs_setup": True})
                user = await asyncio.to_thread(_session_user, request)
                if user is None:
                    return JSONResponse(status_code=401, content={"detail": "login required"})
                request.state.console_user = user
            elif not path.startswith("/v1/"):
                # Non-/v1/ dashboard endpoints (e.g. /conversations, /status,
                # /storage/status) are not auth-gated, but the tenant_context
                # middleware still needs console_user to validate an admin's
                # X-Tenant-Id selector. Set it opportunistically when a valid
                # session cookie is present; absent a session the request
                # proceeds as an anonymous (default-tenant) view.
                user = await asyncio.to_thread(_session_user, request)
                if user is not None:
                    request.state.console_user = user
            return await call_next(request)

        @app.middleware("http")
        async def renew_console_cookie(request: Request, call_next):
            response = await call_next(request)
            token = getattr(request.state, "renew_console_session", "")
            # Login/register/bootstrap issue a new token; logout deletes it.
            # Never overwrite those responses with the previous session.
            if token and request.url.path not in {
                "/api/auth/login", "/api/auth/register", "/api/auth/bootstrap", "/api/auth/logout",
            }:
                _set_console_cookie(response, request, token)
            return response

        @app.middleware("http")
        async def admission_control(request: Request, call_next):
            from ..llm import LLMOverloadedError

            active = getattr(owner, "_active_http_requests", 0)
            limit = max(1, int(os.environ.get("TEAMEVOLVER_HTTP_CONCURRENCY", "256")))
            if active >= limit and request.url.path not in {"/health", "/healthz"}:
                return JSONResponse(status_code=429, content={"detail": "service busy"}, headers={"Retry-After": "2"})
            owner._active_http_requests = active + 1
            try:
                return await call_next(request)
            except LLMOverloadedError:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "model queue full"},
                    headers={"Retry-After": "2"},
                )
            finally:
                owner._active_http_requests -= 1

        @app.get("/readyz")
        async def readiness():
            if bool(getattr(owner.config, "storage_pg_enabled", False)):
                status = await asyncio.to_thread(
                    get_tenant_registry(owner).runtime.pool_status,
                    timeout=15.0,
                )
                return JSONResponse(status_code=200 if status.get("reachable") else 503, content=status)
            return {"ready": True}

        # Skill and user management REST APIs used by the unified console.
        self._register_skills_admin_routes(app)
        self._register_skill_lab_routes(app)
        from .dataset_routes import register_dataset_routes
        register_dataset_routes(self, app)
        self._register_users_admin_routes(app)
        self._register_platform_assets_routes(app)
        self._register_openviking_workspace_routes(app)
        self._register_knowledge_mining_routes(app)
        self._register_memory_debug_routes(app)
        self._register_agent_context_routes(app)
        self._register_skillminer_routes(app)
        self._register_docs_routes(app)
        self._register_aggregation_routes(app)
        register_tenant_routes(self, app)
        from .replay_routes import register_replay_adapter_routes
        register_replay_adapter_routes(self, app)
        from session_ingestion import register_routes as register_ingestion_routes

        register_ingestion_routes(
            self,
            app,
            invalidate_cache=_invalidate_dashboard_cache,
        )

        @app.get("/")
        @app.get("/console")
        async def console():
            if os.path.isfile(dist_index):
                return FileResponse(dist_index)
            return JSONResponse(status_code=404, content={"detail": "teamEvolver console is not built"})

        @app.get("/v1/models")
        async def model_proxy_models(request: Request):
            _check_model_proxy_api_key(request)
            config = _tenant_effective_config(owner)
            model = str(config.llm_model_id or config.model_name or "")
            return {
                "object": "list",
                "data": [
                    {
                        "id": "teamEvolver-model",
                        "object": "model",
                        "owned_by": "teamEvolver",
                        "upstream_model": model,
                    }
                ],
            }

        @app.post("/v1/chat/completions")
        async def model_proxy_chat_completions(request: Request):
            from ..llm import _dispatcher_for

            _check_model_proxy_api_key(request)
            config = _tenant_effective_config(owner)
            payload = _model_proxy_payload(config, await request.json())
            url = _upstream_chat_url(config)
            headers = _upstream_chat_headers(config)
            dispatcher = _dispatcher_for(
                current_tenant_id(),
                int(getattr(config, "llm_max_concurrency", 8) or 8),
                int(getattr(config, "llm_queue_capacity", 64) or 64),
            )
            timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
            if payload.get("stream"):
                slot = await dispatcher.acquire()
                client = httpx.AsyncClient(timeout=timeout)
                try:
                    upstream = await client.send(
                        client.build_request("POST", url, headers=headers, json=payload),
                        stream=True,
                    )
                except BaseException:
                    await client.aclose()
                    dispatcher.release(slot)
                    raise
                if upstream.status_code >= 400:
                    error_body = await upstream.aread()
                    await upstream.aclose()
                    await client.aclose()
                    dispatcher.release(slot)
                    return Response(
                        content=error_body,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get(
                            "content-type", "application/json"
                        ),
                    )

                async def stream_upstream():
                    try:
                        async for chunk in upstream.aiter_raw():
                            yield chunk
                    finally:
                        await upstream.aclose()
                        await client.aclose()
                        dispatcher.release(slot)

                return StreamingResponse(
                    stream_upstream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            slot = await dispatcher.acquire()
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    upstream = await client.post(url, headers=headers, json=payload)
            finally:
                dispatcher.release(slot)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )

        @app.get("/api/auth/status")
        async def auth_status(request: Request):
            user = getattr(request.state, "console_user", None) or await asyncio.to_thread(_session_user, request)
            return {
                "customer_mode": os.environ.get("TEAMEVOLVER_CUSTOMER_MODE") == "1",
                "authenticated": bool(user),
                "needs_setup": await asyncio.to_thread(_users_empty),
                "user": user,
            }

        @app.post("/api/auth/bootstrap")
        async def auth_bootstrap(request: Request):
            if getattr(owner.config, "storage_pg_enabled", False) and not _users_empty():
                _require_admin_user(getattr(request.state, "console_user", None))
            if not _users_empty():
                raise HTTPException(status_code=409, detail="users already exist")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="bootstrap body must be an object")
            password = str(body.get("password") or "admin")
            if getattr(owner.config, "storage_pg_enabled", False) and len(password) < 12:
                raise HTTPException(status_code=400, detail="password must contain at least 12 characters")
            payload = {
                "id": body.get("username") or body.get("id") or "admin",
                "display_name": body.get("display_name") or body.get("username") or "admin",
                "email": body.get("email") or "",
                "role": "admin",
                "password": password,
            }
            path = _registry_path(owner.config)
            data = _load_registry(path, owner.config)
            user = _upsert_user(data, payload, config=owner.config)
            _save_registry(path, data, owner.config)
            sync_openviking_user(owner.config, str(user.get("id") or ""))
            token = await asyncio.to_thread(_issue_console_session, str(user.get("id") or ""))
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            _set_console_cookie(resp, request, token)
            return resp

        @app.post("/api/auth/login")
        async def auth_login(request: Request):
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="login body must be an object")
            username = str(body.get("username") or body.get("id") or "").strip()
            password = str(body.get("password") or "")
            if not username or not password:
                raise HTTPException(status_code=400, detail="username and password are required")
            data = _load_registry(_registry_path(owner.config), owner.config)
            try:
                _idx, user = _find_user(data, username)
            except HTTPException as exc:
                raise HTTPException(status_code=401, detail="invalid username or password") from exc
            if not user.get("password_hash") or not _verify_password(password, str(user.get("password_hash") or "")):
                raise HTTPException(status_code=401, detail="invalid username or password")
            token = await asyncio.to_thread(_issue_console_session, str(user.get("id") or ""))
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            _set_console_cookie(resp, request, token)
            return resp

        @app.post("/api/auth/register")
        async def auth_register(request: Request):
            if _users_empty():
                raise HTTPException(status_code=409, detail="setup required; initialize the admin account first")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="register body must be an object")
            username = str(body.get("username") or body.get("id") or "").strip()
            password = str(body.get("password") or "")
            if not username or not password:
                raise HTTPException(status_code=400, detail="username and password are required")
            path = _registry_path(owner.config)
            data = _load_registry(path, owner.config)
            if any(str(user.get("id") or "") == username for user in data.get("users") or []):
                raise HTTPException(status_code=409, detail="user already exists")
            payload = {
                "id": username,
                "display_name": body.get("display_name") or username,
                "email": body.get("email") or "",
                "role": "user",
                "password": password,
            }
            user = _upsert_user(data, payload, config=owner.config)
            _save_registry(path, data, owner.config)
            sync_openviking_user(owner.config, str(user.get("id") or ""))
            token = await asyncio.to_thread(_issue_console_session, str(user.get("id") or ""))
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            _set_console_cookie(resp, request, token)
            return resp

        @app.post("/api/auth/logout")
        async def auth_logout(request: Request):
            token = _console_session_token(request)
            if token:
                await asyncio.to_thread(_revoke_console_session, token)
            resp = JSONResponse(content={"authenticated": False})
            resp.delete_cookie(_console_cookie_name(request), path="/", secure=request.url.scheme == "https")
            legacy_token = request.cookies.get(_SESSION_COOKIE, "")
            if legacy_token and (
                legacy_token == token or await asyncio.to_thread(_revoke_console_session, legacy_token)
            ):
                resp.delete_cookie(_SESSION_COOKIE, path="/")
            return resp

        @app.get("/api/team-settings")
        async def api_get_team_settings():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            return JSONResponse(
                content=_team_settings_payload(owner.config, store.load())
            )

        @app.post("/api/team-settings")
        async def api_save_team_settings(request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="team settings body must be an object",
                )
            display_name = " ".join(
                str(body.get("display_name") or "").split()
            )
            if not display_name:
                raise HTTPException(
                    status_code=400,
                    detail="display_name is required",
                )
            if len(display_name) > 120:
                raise HTTPException(
                    status_code=400,
                    detail="display_name must be at most 120 characters",
                )

            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            data.setdefault("team", {})["display_name"] = display_name
            store.save(data)
            config = store.to_config()
            config = replace(
                config,
                users_registry_path=str(
                    getattr(owner.config, "users_registry_path", "") or ""
                ),
            )
            await owner._reload_openviking_integrations(config)
            return JSONResponse(
                content=_team_settings_payload(config, data)
            )

        @app.get("/api/model-settings")
        async def api_get_evolve_model():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            # PG config_kv takes priority over YAML for llm section
            pg_llm = _pg_config_get(owner, "llm")
            if pg_llm and isinstance(pg_llm, dict):
                merged = dict(data.get("llm", {}))
                merged.update(pg_llm)
                data["llm"] = merged
            return JSONResponse(
                content=_model_settings_payload(
                    _tenant_effective_config(owner),
                    data,
                )
            )

        @app.post("/api/model-settings")
        async def api_save_evolve_model(request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="model settings body must be an object")

            model = str(body.get("model") or "").strip()
            base_url = str(body.get("base_url") or "").strip()
            provider = str(body.get("provider") or "custom").strip() or "custom"
            if not model:
                raise HTTPException(status_code=400, detail="model is required")
            if not base_url:
                raise HTTPException(status_code=400, detail="base_url is required")
            current_config = _tenant_effective_config(owner)
            try:
                max_tokens = max(1, int(body.get("max_tokens") or current_config.llm_max_tokens or 100000))
                temperature = float(
                    body.get("temperature")
                    if body.get("temperature") is not None
                    else current_config.llm_temperature
                )
                max_concurrency = max(
                    1,
                    min(
                        64,
                        int(
                            body.get("max_concurrency")
                            or current_config.llm_max_concurrency
                            or 8
                        ),
                    ),
                )
                queue_capacity = max(
                    max_concurrency,
                    min(
                        10000,
                        int(
                            body.get("queue_capacity")
                            or current_config.llm_queue_capacity
                            or 64
                        ),
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="invalid model limits or temperature",
                ) from exc
            temperature = max(0.0, min(2.0, temperature))

            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            llm = data.setdefault("llm", {})
            existing_key = str(getattr(current_config, "llm_api_key", "") or "")
            raw_key = body.get("api_key")
            clear_key = bool(body.get("clear_api_key", False))
            api_key = "" if clear_key else existing_key
            if raw_key is not None and str(raw_key).strip():
                api_key = str(raw_key).strip()
            ctx = get_current_tenant()
            if ctx is not None and not ctx.is_default():
                registry = get_tenant_registry(owner)
                overrides = {
                    "llm_provider": provider,
                    "llm_api_base": base_url,
                    "llm_model_id": model,
                    "llm_api_key": api_key,
                    "llm_max_tokens": max_tokens,
                    "llm_temperature": temperature,
                    "llm_max_concurrency": max_concurrency,
                    "llm_queue_capacity": queue_capacity,
                }
                updated = await asyncio.to_thread(
                    registry.update_tenant_config,
                    ctx.tenant_id,
                    overrides,
                )
                if updated is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"unknown tenant: {ctx.tenant_id}",
                    )
                pool = owner._get_engine_pool() if hasattr(owner, "_get_engine_pool") else None
                if pool is not None:
                    await asyncio.to_thread(
                        pool.drop,
                        ctx.tenant_id,
                        reason="tenant model config updated",
                    )
                effective = effective_config(registry, updated, owner.config)
                owner._stop_skillminer()
                return JSONResponse(
                    content=_model_settings_payload(effective, data)
                )
            llm.update(
                {
                    "provider": provider,
                    "api_base": base_url,
                    "model_id": model,
                    "api_key": api_key,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "max_concurrency": max_concurrency,
                    "queue_capacity": queue_capacity,
                }
            )
            store.save(data)
            # Persist to PG config_kv (including API key)
            await asyncio.to_thread(
                _pg_config_put,
                owner,
                "llm",
                {
                    "provider": provider,
                    "api_base": base_url,
                    "model_id": model,
                    "api_key": api_key,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "max_concurrency": max_concurrency,
                    "queue_capacity": queue_capacity,
                },
            )
            await owner._reload_openviking_integrations(store.to_config())
            owner._stop_skillminer()
            return JSONResponse(content=_model_settings_payload(owner.config, data))

        @app.get("/api/skill-evolution/settings")
        async def api_get_evolve_settings():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            # PG config_kv takes priority over YAML for dreamcycle section
            pg_dreamcycle = _pg_config_get(owner, "dreamcycle")
            if pg_dreamcycle and isinstance(pg_dreamcycle, dict):
                merged = dict(data.get("dreamcycle", {}))
                merged.update(pg_dreamcycle)
                data["dreamcycle"] = merged
            return JSONResponse(
                content=_evolve_settings_payload(owner.config, data)
            )

        @app.post("/api/skill-evolution/settings")
        async def api_save_evolve_settings(request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="evolution settings body must be an object",
                )
            evolve_in = body.get("evolve")
            validation_in = body.get("validation")
            memory_in = body.get("memory_maintenance")
            if not isinstance(evolve_in, dict):
                evolve_in = {}
            if not isinstance(validation_in, dict):
                validation_in = {}
            if not isinstance(memory_in, dict):
                memory_in = {}

            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            evolve = data.setdefault("evolve", {})
            validation = data.setdefault("validation", {})
            dreamcycle = data.setdefault("dreamcycle", {})

            def _bounded_int(
                source: dict[str, Any],
                key: str,
                current: Any,
                *,
                minimum: int,
                maximum: int,
            ) -> int:
                raw = source.get(key, current)
                try:
                    value = int(raw)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} must be an integer",
                    ) from exc
                return max(minimum, min(maximum, value))

            for key in (
                "use_session_judge",
                "human_review_enabled",
                "evidence_enabled",
                "dataset_synthesis_enabled",
                "candidate_coalesce_enabled",
                "bundle_allow_delete",
                "bundle_static_checks_enabled",
            ):
                if key in evolve_in:
                    evolve[key] = bool(evolve_in[key])
            publish_mode = str(
                evolve_in.get("publish_mode")
                or evolve.get("publish_mode")
                or "validated"
            ).strip().lower()
            if publish_mode not in {"direct", "validated"}:
                raise HTTPException(
                    status_code=400,
                    detail="publish_mode must be direct or validated",
                )
            evolve["publish_mode"] = publish_mode
            evolve["validation_max_rejections"] = _bounded_int(
                evolve_in,
                "validation_max_rejections",
                evolve.get("validation_max_rejections", 1),
                minimum=1,
                maximum=100,
            )
            evolve["human_review_timeout_seconds"] = _bounded_int(
                evolve_in,
                "human_review_timeout_seconds",
                evolve.get("human_review_timeout_seconds", 86400),
                minimum=1,
                maximum=365 * 86400,
            )
            evolve["interval_seconds"] = _bounded_int(
                evolve_in,
                "interval_seconds",
                evolve.get("interval_seconds", 600),
                minimum=1,
                maximum=365 * 86400,
            )
            evolve["evidence_max_entries"] = _bounded_int(
                evolve_in,
                "evidence_max_entries",
                evolve.get("evidence_max_entries", 400),
                minimum=1,
                maximum=100000,
            )
            evolve["evidence_recent_limit"] = _bounded_int(
                evolve_in,
                "evidence_recent_limit",
                evolve.get("evidence_recent_limit", 20),
                minimum=1,
                maximum=1000,
            )
            evolve["evidence_historical_limit"] = _bounded_int(
                evolve_in,
                "evidence_historical_limit",
                evolve.get("evidence_historical_limit", 20),
                minimum=0,
                maximum=1000,
            )
            evolve["evidence_replay_cases_per_window"] = _bounded_int(
                evolve_in,
                "evidence_replay_cases_per_window",
                evolve.get("evidence_replay_cases_per_window", 1),
                minimum=1,
                maximum=100,
            )
            evolve["evidence_change_debt_threshold"] = _bounded_int(
                evolve_in,
                "evidence_change_debt_threshold",
                evolve.get("evidence_change_debt_threshold", 3),
                minimum=1,
                maximum=100,
            )
            evolve["dataset_test_cases"] = _bounded_int(
                evolve_in,
                "dataset_test_cases",
                evolve.get("dataset_test_cases", 2),
                minimum=1,
                maximum=6,
            )
            evolve["dataset_min_requirements"] = _bounded_int(
                evolve_in,
                "dataset_min_requirements",
                evolve.get("dataset_min_requirements", 12),
                minimum=1,
                maximum=500,
            )
            evolve["dataset_max_requirements"] = _bounded_int(
                evolve_in,
                "dataset_max_requirements",
                evolve.get("dataset_max_requirements", 24),
                minimum=evolve["dataset_min_requirements"],
                maximum=1000,
            )
            evolve["dataset_disclosure_batch_size"] = _bounded_int(
                evolve_in,
                "dataset_disclosure_batch_size",
                evolve.get("dataset_disclosure_batch_size", 4),
                minimum=1,
                maximum=100,
            )
            evolve["bundle_max_file_bytes"] = _bounded_int(
                evolve_in,
                "bundle_max_file_bytes",
                evolve.get("bundle_max_file_bytes", 262144),
                minimum=1024,
                maximum=50 * 1024 * 1024,
            )
            evolve["bundle_max_prompt_bytes"] = _bounded_int(
                evolve_in,
                "bundle_max_prompt_bytes",
                evolve.get("bundle_max_prompt_bytes", 786432),
                minimum=1024,
                maximum=100 * 1024 * 1024,
            )
            if "bundle_text_extensions" in evolve_in:
                raw_extensions = evolve_in.get("bundle_text_extensions")
                if isinstance(raw_extensions, str):
                    raw_extensions = raw_extensions.replace("\n", ",").split(",")
                if not isinstance(raw_extensions, list):
                    raise HTTPException(
                        status_code=400,
                        detail="bundle_text_extensions must be a list or comma-separated string",
                    )
                extensions = []
                for raw in raw_extensions:
                    item = str(raw or "").strip().lower().lstrip(".")
                    if item and "/" not in item and "\\" not in item:
                        extension = f".{item}"
                        if extension not in extensions:
                            extensions.append(extension)
                evolve["bundle_text_extensions"] = extensions or [".py", ".sh"]

            if "enabled" in validation_in:
                validation["enabled"] = bool(validation_in["enabled"])
            mode = str(
                validation_in.get("mode")
                or validation.get("mode")
                or "true_replay"
            ).strip().lower().replace("-", "_")
            if mode not in {"replay", "true_replay"}:
                raise HTTPException(
                    status_code=400,
                    detail="validation mode must be replay or true_replay",
                )
            validation["mode"] = mode
            for key, minimum, maximum, fallback in (
                ("idle_after_seconds", 0, 86400, 300),
                ("poll_interval_seconds", 1, 86400, 60),
                ("max_jobs_per_day", 0, 10000, 5),
                ("max_concurrency", 1, 100, 1),
                ("required_results", 1, 100, 3),
                ("required_approvals", 1, 100, 2),
            ):
                validation[key] = _bounded_int(
                    validation_in,
                    key,
                    validation.get(key, fallback),
                    minimum=minimum,
                    maximum=maximum,
                )
            if validation["required_approvals"] > validation["required_results"]:
                raise HTTPException(
                    status_code=400,
                    detail="required_approvals cannot exceed required_results",
                )

            for key in ("enabled", "auto_start"):
                if key in memory_in:
                    dreamcycle[key] = bool(memory_in[key])
            if "customer_id" in memory_in:
                dreamcycle["customer_id"] = str(
                    memory_in.get("customer_id") or ""
                ).strip()
                dreamcycle.pop("peer_id", None)
            for key, fallback in (
                ("dedup_merge_threshold", 0.86),
                ("dedup_warn_threshold", 0.72),
            ):
                if key not in memory_in:
                    continue
                try:
                    value = float(memory_in[key])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"DreamCycle {key} must be numeric",
                    ) from exc
                dreamcycle[key] = max(-1.0, min(1.0, value))
            if (
                float(dreamcycle.get("dedup_warn_threshold", 0.72))
                > float(dreamcycle.get("dedup_merge_threshold", 0.86))
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "DreamCycle dedup_warn_threshold cannot exceed "
                        "dedup_merge_threshold"
                    ),
                )

            store.save(data)
            # Persist dreamcycle section to PG config_kv
            pg_dreamcycle = dict(dreamcycle)
            await asyncio.to_thread(
                _pg_config_put, owner, "dreamcycle", pg_dreamcycle
            )
            await owner._reload_openviking_integrations(store.to_config())
            return JSONResponse(
                content=_evolve_settings_payload(owner.config, data)
            )



        @app.get("/api/langfuse-tracing-config")
        async def api_get_langfuse_tracing_config():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            return JSONResponse(
                content=_langfuse_tracing_settings_payload(
                    owner.config,
                    store.load(),
                )
            )

        @app.post("/api/langfuse-tracing-config")
        async def api_save_langfuse_tracing_config(request: Request):
            _require_admin_user(
                getattr(request.state, "console_user", None)
                or _session_user(request)
            )
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Langfuse tracing settings body must be an object",
                )

            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            langfuse = data.setdefault("langfuse", {})

            enabled = bool(body.get("enabled", False))
            host = str(
                body.get("host")
                if "host" in body
                else langfuse.get("tracing_host")
                or getattr(owner.config, "langfuse_tracing_host", "")
                or ""
            ).strip().rstrip("/")

            existing_public = str(
                langfuse.get("tracing_public_key")
                or getattr(owner.config, "langfuse_tracing_public_key", "")
                or ""
            )
            existing_secret = str(
                langfuse.get("tracing_secret_key")
                or getattr(owner.config, "langfuse_tracing_secret_key", "")
                or ""
            )
            public_key = (
                ""
                if bool(body.get("clear_public_key", False))
                else existing_public
            )
            secret_key = (
                ""
                if bool(body.get("clear_secret_key", False))
                else existing_secret
            )
            if str(body.get("public_key") or "").strip():
                public_key = str(body["public_key"]).strip()
            if str(body.get("secret_key") or "").strip():
                secret_key = str(body["secret_key"]).strip()

            try:
                sample_rate = float(
                    body.get(
                        "sample_rate",
                        langfuse.get("tracing_sample_rate", 1.0),
                    )
                )
                if not 0.0 <= sample_rate <= 1.0:
                    raise ValueError("sample_rate out of range")
                flush_at = max(
                    1,
                    int(
                        body.get(
                            "flush_at",
                            langfuse.get("tracing_flush_at", 1),
                        )
                    ),
                )
                flush_interval_seconds = max(
                    0.1,
                    float(
                        body.get(
                            "flush_interval_seconds",
                            langfuse.get(
                                "tracing_flush_interval_seconds",
                                1.0,
                            ),
                        )
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="invalid Langfuse tracing sampling or flush settings",
                ) from exc

            if enabled and (not host or not public_key or not secret_key):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "host、public_key 和 secret_key 均为必填项，"
                        "才能启用全局 Langfuse 链路观测"
                    ),
                )

            langfuse.update(
                {
                    "tracing_enabled": enabled,
                    "tracing_host": host,
                    "tracing_public_key": public_key,
                    "tracing_secret_key": secret_key,
                    "tracing_environment": str(
                        body.get(
                            "environment",
                            langfuse.get("tracing_environment", "local"),
                        )
                        or "local"
                    ).strip(),
                    "tracing_release": str(
                        body.get(
                            "release",
                            langfuse.get("tracing_release", ""),
                        )
                        or ""
                    ).strip(),
                    "tracing_sample_rate": sample_rate,
                    "tracing_capture_content": bool(
                        body.get(
                            "capture_content",
                            langfuse.get("tracing_capture_content", True),
                        )
                    ),
                    "tracing_flush_at": flush_at,
                    "tracing_flush_interval_seconds": flush_interval_seconds,
                }
            )
            store.save(data)
            owner._configure_langfuse(store.to_config())
            return JSONResponse(
                content=_langfuse_tracing_settings_payload(
                    owner.config,
                    data,
                )
            )

        @app.post("/api/langfuse-tracing-config/test")
        async def api_test_langfuse_tracing_config(request: Request):
            _require_admin_user(
                getattr(request.state, "console_user", None)
                or _session_user(request)
            )
            from session_ingestion.adapters._shared.langfuse_client import (
                LangfuseClient,
                LangfuseError,
            )

            body = await request.json() if await request.body() else {}
            if not isinstance(body, dict):
                body = {}
            host = str(
                body.get("host")
                or getattr(owner.config, "langfuse_tracing_host", "")
                or ""
            ).strip().rstrip("/")
            public_key = str(
                body.get("public_key")
                or getattr(owner.config, "langfuse_tracing_public_key", "")
                or ""
            ).strip()
            secret_key = str(
                body.get("secret_key")
                or getattr(owner.config, "langfuse_tracing_secret_key", "")
                or ""
            ).strip()
            if not host or not public_key or not secret_key:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "host / public_key / secret_key "
                        "均为测试全局链路观测的必填项"
                    ),
                )
            client = LangfuseClient(
                host=host,
                public_key=public_key,
                secret_key=secret_key,
                timeout=float(owner.config.langfuse_timeout_seconds or 30),
            )
            try:
                health = await asyncio.to_thread(client.health)
            except LangfuseError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                await asyncio.to_thread(client.close)
            return JSONResponse(content=health)







        # ---- Prompt Studio: transparent, editable, testable pipeline ---- #
        def _prompt_studio():
            from team_skills.evolution import prompt_studio as ps

            return ps

        def _load_studio_session(session_id: str) -> dict[str, Any]:
            """Load a full session dict (turns) to use as test input."""
            store = SessionStore.from_config(
                _tenant_effective_config(owner), tenant_id=current_tenant_id()
            )
            session = store.load_session(_safe_session_id(session_id))
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
            return session

        def _studio_llm_factory():
            from ..llm import AsyncLLMClient

            config = _tenant_effective_config(owner)
            api_key = str(getattr(config, "llm_api_key", "") or "")
            base_url = str(getattr(config, "llm_api_base", "") or "")
            model = str(getattr(config, "llm_model_id", "") or getattr(config, "model_name", "") or "")
            return AsyncLLMClient(
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                model=model or "not-configured",
                max_tokens=int(getattr(config, "llm_max_tokens", 100000) or 100000),
                temperature=float(getattr(config, "llm_temperature", 0.4) or 0.4),
                tenant_id=current_tenant_id(),
                max_concurrency=int(
                    getattr(config, "llm_max_concurrency", 8) or 8
                ),
                queue_capacity=int(
                    getattr(config, "llm_queue_capacity", 64) or 64
                ),
            )

        @app.get("/api/skill-evolution/pipeline")
        async def api_prompt_studio_pipeline():
            return JSONResponse(
                content=await asyncio.to_thread(
                    _prompt_studio().pipeline_graph,
                    _tenant_effective_config(owner),
                )
            )

        @app.get("/api/skill-evolution/prompts")
        async def api_prompt_studio_prompts():
            return JSONResponse(
                content={
                    "prompts": await asyncio.to_thread(
                        _prompt_studio().list_prompts,
                        _tenant_effective_config(owner),
                    )
                }
            )

        @app.get("/api/skill-evolution/prompts/{stage_id}")
        async def api_prompt_studio_prompt_detail(stage_id: str):
            try:
                return JSONResponse(
                    content=await asyncio.to_thread(
                        _prompt_studio().get_prompt,
                        stage_id,
                        _tenant_effective_config(owner),
                    )
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc

        @app.post("/api/skill-evolution/prompts/{stage_id}")
        async def api_prompt_studio_save(stage_id: str, request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be an object")
            ps = _prompt_studio()
            config = _tenant_effective_config(owner)
            try:
                if "prompt" in body:
                    ps.set_override(stage_id, str(body.get("prompt") or ""), config=config)
                if "settings" in body:
                    ps.set_stage_settings(stage_id, body.get("settings"), config=config)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(content=ps.get_prompt(stage_id, config))

        @app.post("/api/skill-evolution/prompts/{stage_id}/reset")
        async def api_prompt_studio_reset(stage_id: str, request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            ps = _prompt_studio()
            config = _tenant_effective_config(owner)
            try:
                ps.reset_override(stage_id, config=config)
                ps.reset_stage_settings(stage_id, config=config)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            return JSONResponse(content=ps.get_prompt(stage_id, config))

        @app.get("/api/skill-evolution/session-samples")
        async def api_prompt_studio_sessions(limit: int = 20):
            """Recent sessions the operator can use as test input."""
            try:
                store = SessionStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                rows = store.list_conversations(limit=max(1, min(200, int(limit or 20))))
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(content={"sessions": [], "reason": str(exc)})
            sessions = [
                {
                    "session_id": r.get("session_id"),
                    "title": r.get("title"),
                    "user_alias": r.get("user_alias"),
                    "num_turns": r.get("num_turns"),
                    "status": r.get("status"),
                    "timestamp": r.get("timestamp") or r.get("ingested_at"),
                }
                for r in rows
            ]
            return JSONResponse(content={"sessions": sessions})

        @app.post("/api/skill-evolution/prompts/{stage_id}/test")
        async def api_prompt_studio_test(stage_id: str, request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be an object")
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                raise HTTPException(status_code=400, detail="session_id is required for a prompt test")
            system_prompt = body.get("prompt")
            skill_name = str(body.get("skill_name") or "").strip()
            ps = _prompt_studio()
            session = _load_studio_session(session_id)
            if skill_name:
                session = dict(session)
                session["_probe_skill_name"] = skill_name
            try:
                result = await ps.run_stage_test(
                    stage_id,
                    session,
                    system_prompt=(str(system_prompt) if system_prompt is not None else None),
                    llm_factory=_studio_llm_factory,
                    config=_tenant_effective_config(owner),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"prompt test failed: {exc}") from exc
            return JSONResponse(content=result)

        @app.post("/api/model-settings/test")
        async def api_test_evolve_model(request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            store = ConfigStore()
            data = store.load()
            llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
            config = _tenant_effective_config(owner)
            base_url = str(body.get("base_url") or config.llm_api_base or llm.get("api_base") or "").strip()
            model = str(body.get("model") or config.llm_model_id or llm.get("model_id") or "").strip()
            raw_key = body.get("api_key")
            api_key = (
                str(raw_key).strip()
                if raw_key is not None and str(raw_key).strip()
                else str(config.llm_api_key or llm.get("api_key") or "")
            )
            if not base_url or not model or not api_key:
                raise HTTPException(status_code=400, detail="base_url, model and api_key are required for test")
            try:
                from ..llm import _call_in_pool, _dispatcher_for
                from openai import OpenAI

                started = time.time()
                dispatcher = _dispatcher_for(
                    current_tenant_id(),
                    int(getattr(config, "llm_max_concurrency", 8) or 8),
                    int(getattr(config, "llm_queue_capacity", 64) or 64),
                )

                def call_model():
                    client = OpenAI(api_key=api_key, base_url=base_url)
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are a connectivity test endpoint."},
                            {"role": "user", "content": "Reply with exactly: ok"},
                        ],
                        "max_completion_tokens": 16,
                        "temperature": 0,
                    }
                    try:
                        return client.chat.completions.create(**payload)
                    except Exception as first_exc:
                        body_text = getattr(
                            getattr(first_exc, "response", None),
                            "text",
                            "",
                        ) or ""
                        if "'temperature' is not supported" in body_text:
                            payload.pop("temperature", None)
                            return client.chat.completions.create(**payload)
                        if "max_completion_tokens" in body_text:
                            payload["max_tokens"] = payload.pop(
                                "max_completion_tokens"
                            )
                            return client.chat.completions.create(**payload)
                        raise

                resp = await _call_in_pool(
                    call_model,
                    dispatcher=dispatcher,
                )
                message = resp.choices[0].message
                content = getattr(message, "content", None) or getattr(message, "reasoning_content", None) or ""
                return {
                    "ok": True,
                    "model": model,
                    "base_url": base_url,
                    "latency_ms": int((time.time() - started) * 1000),
                    "response": content[:200],
                }
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                body_text = getattr(getattr(exc, "response", None), "text", "") or ""
                if body_text:
                    detail = body_text[:1000]
                raise HTTPException(status_code=400, detail=f"model test failed: {detail}") from exc











        async def _register_agent_runtime(body: dict[str, Any]) -> dict[str, Any]:
            """Register an Agent and optionally merge cloud OpenViking sources.

            The durable teamEvolver config is authoritative. In local deployment
            mode an external Agent may register capabilities/endpoints, but it
            cannot replace the local OpenViking endpoint or credentials.
            """
            try:
                agent = register_agent(owner.config, body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            registration_fields: dict[str, Any] = {
                "agent": public_agent_record(agent),
            }
            raw_subject_mappings = body.get("subject_mappings")
            if is_v1_payload(body) and raw_subject_mappings is not None:
                if not isinstance(raw_subject_mappings, list):
                    raise HTTPException(
                        status_code=400,
                        detail="subject_mappings must be a list",
                    )
                try:
                    registration_fields["subject_sync"] = (
                        sync_agent_subject_mappings(
                            owner.config,
                            integration_id=str(agent.get("agent_id") or ""),
                            runtime_type=str(agent.get("runtime_type") or ""),
                            mappings=raw_subject_mappings,
                            authoritative=bool(
                                body.get(
                                    "subject_mappings_authoritative",
                                    False,
                                )
                            ),
                        )
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=str(exc),
                    ) from exc

            storage = body.get("storage") if isinstance(body.get("storage"), dict) else {}
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            data = store.load()
            sharing = data.setdefault("sharing", {})
            deployment = str(sharing.get("viking_deployment") or "cloud").strip().lower()
            durable_config = store.to_config()
            if deployment == "local":
                current_endpoint = str(
                    getattr(owner.config, "sharing_viking_endpoint", "") or ""
                ).rstrip("/")
                durable_endpoint = str(
                    durable_config.sharing_viking_endpoint or ""
                ).rstrip("/")
                if current_endpoint != durable_endpoint:
                    await owner._reload_openviking_integrations(durable_config)
                return {
                    "ok": True,
                    **registration_fields,
                    "storage_authority": "teamEvolver",
                    "storage_updated": False,
                    "storage_ignored_reason": "local_deployment_is_authoritative",
                    "account": durable_config.sharing_viking_account,
                    "team_user": durable_config.sharing_viking_user,
                }

            endpoint = str(storage.get("endpoint") or "").strip()
            account = str(storage.get("account") or "").strip()
            personal_user = str(storage.get("personal_user") or "").strip()
            configured_team_user = str(storage.get("team_user") or "").strip()
            team_key = str(
                storage.get("service_api_key") or storage.get("team_api_key") or ""
            ).strip()
            if not endpoint and not team_key:
                return {
                    "ok": True,
                    **registration_fields,
                    "storage_authority": "teamEvolver",
                    "storage_updated": False,
                }
            if not endpoint or not team_key:
                raise HTTPException(
                    status_code=400,
                    detail="storage.endpoint and storage.service_api_key must be provided together",
                )

            from team_memory.maintenance.integrations.dreamcycle import parse_openviking_key

            key_account, encoded_team_user = parse_openviking_key(team_key)
            team_user = encoded_team_user or configured_team_user
            if not team_user:
                raise HTTPException(
                    status_code=400,
                    detail="storage.team_user is required when the service key has no encoded user",
                )
            sharing.update(
                {
                    "enabled": True,
                    "backend": "viking",
                    "viking_endpoint": endpoint,
                    "viking_account": key_account or account,
                    "viking_personal_user": personal_user,
                    "viking_user": team_user,
                    "viking_team_api_key": team_key,
                }
            )
            sharing.pop("viking_personal_api_key", None)
            sharing.pop("viking_personal_api_keys", None)
            # DreamCycle is retired in favor of the ov compile-based cross-user
            # memory aggregation in team_memory, so agent
            # registration no longer auto-enables or auto-starts it.
            for key in (
                "viking_endpoint",
                "viking_api_key",
                "viking_account",
                "viking_team_user",
            ):
                data["dreamcycle"].pop(key, None)
            store.save(data)
            config = store.to_config()
            await owner._reload_openviking_integrations(config)
            return {
                "ok": True,
                **registration_fields,
                "storage_authority": "shared_agent_config",
                "storage_updated": True,
                "account": key_account or account,
                "team_user": team_user,
            }

        @app.post("/internal/agents/register")
        async def register_agent_runtime(request: Request):
            body = await _read_limited_json_body(request)
            if is_v1_payload(body):
                _check_v1_control_plane_key(request)
            else:
                _check_ingest_api_key(request)
            return await _register_agent_runtime(body)

        @app.post("/internal/agentshub/openviking-config")
        async def sync_agentshub_openviking_config(request: Request):
            """Backward-compatible adapter for older AgentsHub builds."""
            _check_ingest_api_key(request)
            body = await _read_limited_json_body(request)
            return await _register_agent_runtime(
                {
                    "agent_id": str(body.get("agent_id") or "agentshub"),
                    "runtime_type": "agentshub",
                    "display_name": "AgentsHub",
                    "capabilities": [
                        "session_ingest",
                        "true_replay",
                        "skill_sync",
                        "openviking_context",
                    ],
                    "endpoints": body.get("endpoints") or {},
                    "metadata": body.get("metadata") or {},
                    "storage": {
                        "endpoint": body.get("endpoint"),
                        "account": body.get("account"),
                        "personal_user": body.get("personal_user"),
                        "team_user": body.get("team_user"),
                        "team_api_key": body.get("team_api_key"),
                    },
                }
            )

        @app.get("/api/agent-integrations")
        async def api_agent_integrations():
            config = _tenant_effective_config(owner)
            skill_outbox = (
                await asyncio.to_thread(
                    SkillMutationService.from_config(config, tenant_id=current_tenant_id()).health
                )
                if getattr(config, "sharing_enabled", False)
                else {
                    "backlog": 0,
                    "oldest_age_seconds": 0,
                    "dead_letter": 0,
                    "last_error": "",
                }
            )
            return {
                "agents": [
                    public_agent_record(agent)
                    for agent in list_agents(owner.config)
                ],
                "skills_delivery_mode": getattr(config, "skills_delivery_mode", "push"),
                "skill_sync_outbox": skill_outbox,
                "storage_authority": "teamEvolver",
                "storage_deployment": str(
                    getattr(owner.config, "sharing_viking_deployment", "") or "cloud"
                ),
                "default_team_user": str(
                    getattr(owner.config, "sharing_viking_user", "") or "team"
                ),
            }

        @app.post("/api/agent-integrations")
        async def api_register_agent_integration(request: Request):
            """Console-side Agent registration (admin session auth).

            Builds a V1 registration payload from simple form fields and
            reuses the same registration path as the control-plane endpoint.
            ``auth_profile`` left empty means the replay endpoint is called
            without a Bearer key (trusted-network deployments)."""
            _require_admin_user(getattr(request.state, "console_user", None))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be an object")
            agent_id = str(body.get("agent_id") or "").strip()
            runtime_type = str(body.get("runtime_type") or "").strip()
            replay_url = str(body.get("replay_url") or "").strip()
            if not agent_id or not runtime_type or not replay_url:
                raise HTTPException(
                    status_code=400,
                    detail="agent_id, runtime_type and replay_url are required",
                )
            try:
                max_interactions = max(1, min(20, int(body.get("max_interactions") or 10)))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail="max_interactions must be an integer"
                ) from exc
            capability: dict[str, Any] = {
                "transport": "http",
                "orchestration": str(body.get("orchestration") or "server_driven"),
                "endpoint": replay_url,
                "max_interactions": max_interactions,
            }
            auth_profile = str(body.get("auth_profile") or "").strip()
            if auth_profile:
                capability["auth_profile"] = auth_profile
            # Zero agent-side awareness mode: render turns into the agent's
            # own request shape and extract results via dotted-path mapping.
            request_template = body.get("request_template")
            if isinstance(request_template, dict) and request_template:
                capability["request_template"] = request_template
            response_mapping = body.get("response_mapping")
            if isinstance(response_mapping, dict) and response_mapping:
                capability["response_mapping"] = response_mapping
            capabilities: dict[str, Any] = {"replay.branch.v1": capability}
            if bool(body.get("session_ingest", False)):
                capabilities["session.ingest.v1"] = {}
            payload = {
                "schema_version": "teamevolver.agent-registration.v1",
                "protocol_version": "1.0",
                "agent_id": agent_id,
                "runtime_type": runtime_type,
                "runtime_version": str(body.get("runtime_version") or "1.0.0"),
                "display_name": str(body.get("display_name") or agent_id),
                "capabilities": capabilities,
                "endpoints": {"replay_url": replay_url},
            }
            return await _register_agent_runtime(payload)

        @app.post("/api/agent-integrations/skill-sync/{event_id}/retry")
        async def api_retry_skill_sync(
            event_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            _require_admin_user(getattr(request.state, "console_user", None))
            config = _tenant_effective_config(owner)
            if getattr(config, "skills_delivery_mode", "push") == "pull":
                raise HTTPException(status_code=409, detail="delivery_mode_changed_to_pull")
            try:
                event = await asyncio.to_thread(
                    SkillMutationService.from_config(config, tenant_id=current_tenant_id()).retry,
                    event_id,
                    integration_id=str(body.get("integration_id") or ""),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="event not found") from exc
            return {
                "event_id": event_id,
                "status": event.get("status"),
            }

        @app.post("/api/agent-integrations/skill-sync/{event_id}/discard")
        async def api_discard_skill_sync(
            event_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            admin = getattr(request.state, "console_user", None)
            _require_admin_user(admin)
            try:
                event = await asyncio.to_thread(
                    SkillMutationService.from_config(
                        _tenant_effective_config(owner), tenant_id=current_tenant_id(),
                    ).discard,
                    event_id,
                    integration_id=str(body.get("integration_id") or ""),
                    actor=str(
                        (admin or {}).get("username")
                        or (admin or {}).get("id")
                        or "admin"
                    ),
                    reason=str(body.get("reason") or ""),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="event not found") from exc
            return {
                "event_id": event_id,
                "status": event.get("status"),
            }

        @app.get("/api/agent-protocol/metrics")
        async def agent_protocol_metrics(request: Request):
            if not getattr(request.state, "service_root_authenticated", False):
                raise HTTPException(status_code=403, detail="service root key required")
            from ..integrations.protocol_metrics import render

            return Response(content=render(), media_type="text/plain; version=0.0.4")

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        from team_memory.memory_routes import register_replay_routes

        register_replay_routes(owner, app, session_user=_session_user)

        @app.get("/storage/status")
        async def storage_status():
            return JSONResponse(
                content=await asyncio.to_thread(
                    _storage_status,
                    _tenant_effective_config(owner),
                    owner,
                )
            )

        @app.get("/api/sharing-config")
        async def api_get_sharing_config():
            store = ConfigStore()
            data = store.load()
            effective = _tenant_effective_config(owner)
            sharing = data.get("sharing", {}) if isinstance(data.get("sharing"), dict) else {}
            deployment = str(
                getattr(effective, "sharing_viking_deployment", "") or "cloud"
            ).strip().lower()
            if deployment not in {"cloud", "local"}:
                deployment = "cloud"
            service_api_key_present = bool(
                getattr(effective, "sharing_viking_team_api_key", "")
                or getattr(effective, "sharing_viking_api_key", "")
            )
            return JSONResponse(
                content={
                    "enabled": bool(getattr(effective, "sharing_enabled", True)),
                    "backend": "viking",
                    "deployment": deployment,
                    "endpoint": str(
                        getattr(effective, "sharing_viking_endpoint", "") or ""
                    ),
                    "endpoint_override": str(
                        getattr(effective, "sharing_viking_endpoint", "")
                        or sharing.get("viking_endpoint", "")
                        or ""
                    ),
                    "cloud_endpoint": VOLCENGINE_OPENVIKING_ENDPOINT,
                    "local_endpoint": LOCAL_OPENVIKING_ENDPOINT,
                    "account": str(
                        getattr(effective, "sharing_viking_account", "") or "default"
                    ),
                    "team_user": str(
                        getattr(effective, "sharing_viking_user", "") or "team"
                    ),
                    "root_prefix": str(
                        getattr(effective, "sharing_viking_root_prefix", "")
                        or "team-skill-evolver"
                    ),
                    "service_api_key_present": service_api_key_present,
                    "team_api_key_present": service_api_key_present,
                }
            )

        @app.post("/api/sharing-config")
        async def api_save_sharing_config(request: Request):
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="sharing settings body must be an object")
            deployment = str(body.get("deployment") or "cloud").strip().lower()
            if deployment not in {"cloud", "local"}:
                raise HTTPException(
                    status_code=400, detail="deployment must be 'cloud' or 'local'"
                )

            # Per-tenant fields that live in tenant config_overrides (not global
            # config.yaml).  For the default tenant these fall through to the
            # global store below.
            ctx = get_current_tenant()
            is_non_default = (
                ctx is not None
                and not ctx.is_default()
                and bool(getattr(owner.config, "storage_pg_enabled", False))
            )

            if is_non_default:
                registry = get_tenant_registry(owner)
                overrides: dict[str, Any] = {
                    "sharing_viking_personal_user": None,
                    "sharing_viking_personal_api_key": None,
                    "sharing_viking_personal_api_keys": None,
                }
                if "account" in body:
                    overrides["sharing_viking_account"] = str(
                        body.get("account") or "default"
                    ).strip()
                if "endpoint_override" in body:
                    overrides["sharing_viking_endpoint"] = str(
                        body.get("endpoint_override") or ""
                    ).strip()
                if "service_api_key" in body or "team_api_key" in body:
                    overrides["sharing_viking_team_api_key"] = str(
                        body.get("service_api_key") or body.get("team_api_key") or ""
                    ).strip()
                updated_ctx = ctx
                if overrides:
                    updated_ctx = (
                        await asyncio.to_thread(
                            registry.update_tenant_config, ctx.tenant_id, overrides
                        )
                        or ctx
                    )
                    # Drop the resident engine so it rebuilds with the new config.
                    pool = owner._get_engine_pool() if hasattr(owner, "_get_engine_pool") else None
                    if pool is not None:
                        await asyncio.to_thread(pool.drop, ctx.tenant_id, reason="sharing-config updated")
                    await owner._reload_openviking_integrations(
                        _tenant_effective_config(owner)
                    )
                # Directory bootstrap: the account may have just been
                # (re-)pointed at an empty namespace. Idempotent and
                # fail-open; existing directories are kept untouched. The
                # request-scoped context still carries the pre-save overrides,
                # so the freshly updated one drives the bootstrap.
                payload = _storage_status(
                    _tenant_effective_config(owner),
                    owner,
                )
                payload["openviking_dirs"] = await asyncio.to_thread(
                    ensure_openviking_dirs,
                    effective_config(None, updated_ctx, owner.config),
                )
                return JSONResponse(content=payload)

            # Default tenant / single-tenant: write to global config.yaml.
            store = ConfigStore()
            data = store.load()
            sharing = data.setdefault("sharing", {})
            sharing["enabled"] = bool(body.get("enabled", sharing.get("enabled", True)))
            sharing["backend"] = "viking"
            sharing["viking_deployment"] = deployment
            if "endpoint_override" in body:
                sharing["viking_endpoint"] = str(body.get("endpoint_override") or "").strip()
            if "account" in body:
                sharing["viking_account"] = str(body.get("account") or "default").strip()
            sharing.pop("viking_personal_user", None)
            sharing.pop("viking_personal_api_key", None)
            sharing.pop("viking_personal_api_keys", None)
            if "service_api_key" in body or "team_api_key" in body:
                sharing["viking_team_api_key"] = str(
                    body.get("service_api_key") or body.get("team_api_key") or ""
                ).strip()
            store.save(data)
            owner.config = store.to_config()
            await owner._reload_openviking_integrations(owner.config)
            # Directory bootstrap for the just-saved connection (idempotent,
            # fail-open, existing directories untouched).
            status = _storage_status(owner.config, owner)
            status["openviking_dirs"] = await asyncio.to_thread(
                ensure_openviking_dirs, owner.config
            )
            return JSONResponse(content=status)

        @app.post("/api/sharing-config/bootstrap-dirs")
        async def api_bootstrap_openviking_dirs(request: Request):
            """Check and create the OpenViking directory skeleton on demand.

            Repairs accounts created before the automatic bootstrap existed (or
            a namespace wiped on the OpenViking side). Admin-only; an optional
            ``{"account": "..."}`` body overrides the configured account for
            this check only.
            """
            _require_admin_user(
                getattr(request.state, "console_user", None) or _session_user(request)
            )
            effective = _tenant_effective_config(owner)
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 - empty body is fine
                body = None
            account = str((body or {}).get("account") or "").strip() if isinstance(body, dict) else ""
            report = await asyncio.to_thread(
                ensure_openviking_dirs, effective, account_id=account
            )
            return JSONResponse(content={"openviking_dirs": report})

        @app.get("/status")
        async def dashboard_status(refresh: bool = False):
            cache_key = f"status:{id(owner.config)}"
            if refresh:
                _invalidate_dashboard_cache(cache_key)

            def build_status():
                skills: dict[str, dict[str, Any]] = {}
                effective_cfg = _tenant_effective_config(owner)
                session_queue = _session_queue_snapshot(effective_cfg, limit=0)
                cache_status: dict[str, Any] = {}
                outbox_status: dict[str, Any] = {}
                skills_error = ""
                try:
                    hub = SkillHub.team_from_config(effective_cfg, tenant_id=current_tenant_id())
                    for item in hub.list_remote():
                        name = str(item.get("name") or "")
                        if not name:
                            continue
                        skills[name] = {
                            "skill_id": item.get("skill_id") or name,
                            "version": item.get("version") or 0,
                        }
                    cache_status = _skill_cache_status(owner, hub)
                    outbox_status = SkillMutationService.from_hub(
                        hub,
                        config=effective_cfg,
                    ).health()
                except Exception as exc:  # noqa: BLE001
                    skills_error = str(exc)

                from ..integrations.skillopt_rollout import supervisor_status

                rollout = supervisor_status()
                return {
                    "running": False,
                    "instance_id": _INSTANCE_ID,
                    "tenant_id": current_tenant_id(),
                    "pending_sessions": int(session_queue.get("pending") or 0),
                    "registered_skills": len(skills),
                    "skills": skills,
                    "skills_error": skills_error,
                    **cache_status,
                    "outbox": outbox_status,
                    "rollout_supervisor": {
                        "running": bool(rollout.get("running")),
                        "updated_at": str(rollout.get("updated_at") or ""),
                        "tenant": dict(
                            (rollout.get("tenants") or {}).get(
                                current_tenant_id()
                            )
                            or {}
                        ),
                    },
                }

            # Sync viking I/O must not run on the event loop: one slow call
            # would stall every concurrent request.
            return await asyncio.to_thread(
                _cached_dashboard_value, cache_key, 5.0, build_status
            )

        @app.get("/sessions")
        async def dashboard_sessions(
            limit: int = 20,
            offset: int = 0,
            refresh: bool = False,
        ):
            safe_limit = min(200, max(1, int(limit or 20)))
            safe_offset = max(0, int(offset or 0))
            cache_key = f"queue:{id(owner.config)}"
            if refresh:
                _invalidate_dashboard_cache(cache_key, f"status:{id(owner.config)}")
            rows = await asyncio.to_thread(
                _cached_dashboard_value,
                cache_key,
                5.0,
                lambda: SessionStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                ).list_queue(limit=100000),
            )
            page = rows[safe_offset : safe_offset + safe_limit]
            return {
                "reachable": True,
                "sessions": page,
                "pending": len(rows),
                "total": len(rows),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": safe_offset + len(page) < len(rows),
            }

        @app.get("/conversations")
        async def dashboard_conversations(
            limit: int = 20,
            offset: int = 0,
            refresh: bool = False,
            search: str = "",
            status: str = "",
            decision: str = "",
            case: str = "",
            skill: str = "",
            start: str = "",
            end: str = "",
            sort_by: str = "",
            order: str = "",
        ):
            try:
                safe_limit = min(200, max(1, int(limit or 20)))
                safe_offset = max(0, int(offset or 0))
                cache_key = f"conversations:{id(owner.config)}"
                if refresh:
                    _invalidate_dashboard_cache(cache_key)
                try:
                    conversations = await asyncio.wait_for(
                        asyncio.to_thread(
                            _cached_dashboard_value,
                            cache_key,
                            15.0,
                            lambda: SessionStore.from_config(
                                _tenant_effective_config(owner), tenant_id=current_tenant_id()
                            ).list_conversations(
                                limit=100000
                            ),
                        ),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[Conversations] list_conversations timed out (60s)")
                    conversations = []
                # Enrich BEFORE filtering so the ``case`` filter can match on
                # judge scores; index build + lookups are cache-backed/cheap.
                try:
                    score_index = await asyncio.wait_for(
                        asyncio.to_thread(
                            _cached_dashboard_value,
                            f"session-judge-scores:{id(owner.config)}",
                            30.0,
                            lambda: _session_judge_score_index(_tenant_effective_config(owner)),
                        ),
                        timeout=20.0,
                    )
                    for row in conversations:
                        judge = score_index.get(str(row.get("session_id") or ""))
                        if judge:
                            row["judge"] = judge
                except asyncio.TimeoutError:
                    logger.warning("[Conversations] judge score enrichment timed out (20s)")
                except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                    logger.warning("[Conversations] judge score enrichment failed: %s", exc)
                filtered = _filter_conversation_rows(
                    conversations,
                    search=search,
                    status=status,
                    decision=decision,
                    case=case,
                    skill=skill,
                    start=start,
                    end=end,
                )
                filtered = _sort_conversation_rows(filtered, sort_by=sort_by, order=order)
                page = filtered[safe_offset : safe_offset + safe_limit]
                skill_counts: dict[str, int] = {}
                for row in filtered:
                    used = row.get("used_skills") if isinstance(row.get("used_skills"), list) else []
                    for name in used:
                        name = str(name).strip()
                        if name:
                            skill_counts[name] = skill_counts.get(name, 0) + 1
                return {
                    "reachable": True,
                    "conversations": page,
                    "total": len(filtered),
                    "stats": _conversation_stats(filtered),
                    "skill_counts": dict(
                        sorted(skill_counts.items(), key=lambda kv: kv[1], reverse=True)[:100]
                    ),
                    "limit": safe_limit,
                    "offset": safe_offset,
                    "has_more": safe_offset + len(page) < len(filtered),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "reachable": False,
                    "conversations": [],
                    "total": 0,
                    "reason": str(exc),
                }

        @app.get("/conversations/export")
        async def dashboard_conversations_export(
            ids: str = "",
            search: str = "",
            status: str = "",
            decision: str = "",
            case: str = "",
            skill: str = "",
            start: str = "",
            end: str = "",
            sort_by: str = "",
            order: str = "",
            format: str = "json",
            limit: int = 5000,
        ):
            """Standalone batch export with quality details.

            Explicit ``ids`` (comma separated, checkbox selection) take
            precedence; otherwise the same server-side filters as the list
            endpoint are applied. Supports ``format=json`` (default) and
            ``format=csv`` (UTF-8 BOM so Excel opens Chinese correctly).
            """
            try:
                safe_limit = min(20000, max(1, int(limit or 5000)))
                conversations = await asyncio.to_thread(
                    _cached_dashboard_value,
                    f"conversations:{id(owner.config)}",
                    15.0,
                    lambda: SessionStore.from_config(
                        _tenant_effective_config(owner), tenant_id=current_tenant_id()
                    ).list_conversations(
                        limit=100000
                    ),
                )
                try:
                    score_index = await asyncio.to_thread(
                        _cached_dashboard_value,
                        f"session-judge-scores:{id(owner.config)}",
                        30.0,
                        lambda: _session_judge_score_index(_tenant_effective_config(owner)),
                    )
                    for row in conversations:
                        judge = score_index.get(str(row.get("session_id") or ""))
                        if judge:
                            row["judge"] = judge
                except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                    logger.warning("[Export] judge score enrichment failed: %s", exc)

                wanted_ids = [
                    _safe_session_id(value.strip())
                    for value in str(ids or "").split(",")
                    if value.strip()
                ]
                wanted_ids = [value for value in wanted_ids if value]
                if wanted_ids:
                    by_id = {str(row.get("session_id") or ""): row for row in conversations}
                    selected = [by_id[value] for value in wanted_ids if value in by_id]
                    skipped_ids = [value for value in wanted_ids if value not in by_id]
                else:
                    selected = _filter_conversation_rows(
                        conversations,
                        search=search,
                        status=status,
                        decision=decision,
                        case=case,
                        skill=skill,
                        start=start,
                        end=end,
                    )
                    selected = _sort_conversation_rows(selected, sort_by=sort_by, order=order)
                    skipped_ids = []
                selected = selected[:safe_limit]

                def _quality(row: dict[str, Any]) -> dict[str, Any]:
                    value_judge = (
                        row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
                    )
                    judge = row.get("judge") if isinstance(row.get("judge"), dict) else {}
                    score = judge.get("overall_score")
                    return {
                        "value_judge": {
                            "decision": value_judge.get("decision"),
                            "confidence": value_judge.get("confidence"),
                            "reason": value_judge.get("reason"),
                        },
                        "judge": {
                            "overall_score": score,
                            "case_type": (
                                "good"
                                if isinstance(score, (int, float))
                                and not isinstance(score, bool)
                                and float(score) >= _GOOD_CASE_SCORE
                                else "bad"
                                if isinstance(score, (int, float))
                                and not isinstance(score, bool)
                                else None
                            ),
                            "judged_at": judge.get("judged_at"),
                            "rationale": judge.get("rationale"),
                            "reasons": _clean_judge_reasons(judge.get("reasons")),
                        },
                    }

                export_format = str(format or "json").strip().lower()
                if export_format == "csv":
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
                    writer.writerow(
                        [
                            "session_id",
                            "title",
                            "user_alias",
                            "status",
                            "num_turns",
                            "used_skills",
                            "timestamp",
                            "ingested_at",
                            "value_judge_decision",
                            "value_judge_confidence",
                            "value_judge_reason",
                            "judge_overall_score",
                            "judge_case_type",
                            "judge_judged_at",
                            "judge_rationale",
                            "judge_task_completion_reasons",
                            "judge_response_quality_reasons",
                            "judge_efficiency_reasons",
                            "judge_tool_usage_reasons",
                        ]
                    )
                    for row in selected:
                        quality = _quality(row)
                        value_judge = quality["value_judge"]
                        judge = quality["judge"]
                        reasons = judge.get("reasons") or {}
                        writer.writerow(
                            [
                                row.get("session_id") or "",
                                row.get("title") or "",
                                row.get("user_alias") or "",
                                row.get("status") or "",
                                row.get("num_turns") if row.get("num_turns") is not None else "",
                                ";".join(
                                    str(s) for s in (row.get("used_skills") or []) if s
                                ),
                                row.get("timestamp") or "",
                                row.get("ingested_at") or "",
                                value_judge.get("decision") or "",
                                value_judge.get("confidence")
                                if value_judge.get("confidence") is not None
                                else "",
                                value_judge.get("reason") or "",
                                judge.get("overall_score")
                                if judge.get("overall_score") is not None
                                else "",
                                judge.get("case_type") or "",
                                judge.get("judged_at") or "",
                                judge.get("rationale") or "",
                                "; ".join(reasons.get("task_completion") or []),
                                "; ".join(reasons.get("response_quality") or []),
                                "; ".join(reasons.get("efficiency") or []),
                                "; ".join(reasons.get("tool_usage") or []),
                            ]
                        )
                    payload = "\ufeff" + buffer.getvalue()
                    return Response(
                        content=payload,
                        media_type="text/csv; charset=utf-8",
                        headers={
                            "Content-Disposition": 'attachment; filename="sessions_export.csv"',
                            "X-Export-Count": str(len(selected)),
                        },
                    )

                return {
                    "reachable": True,
                    "total": len(selected),
                    "capped": len(selected) >= safe_limit,
                    "skipped_ids": skipped_ids,
                    "sessions": [
                        {
                            "session_id": row.get("session_id") or "",
                            "title": row.get("title") or "",
                            "user_alias": row.get("user_alias") or "",
                            "status": row.get("status") or "",
                            "num_turns": row.get("num_turns"),
                            "used_skills": row.get("used_skills") or [],
                            "timestamp": row.get("timestamp") or "",
                            "ingested_at": row.get("ingested_at") or "",
                            **_quality(row),
                        }
                        for row in selected
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                logger.error("[Export] conversations export failed: %s", exc)
                if str(format or "").strip().lower() == "csv":
                    return Response(
                        content="\ufefferror\n" + str(exc).replace(",", " "),
                        media_type="text/csv; charset=utf-8",
                        status_code=500,
                    )
                return JSONResponse(status_code=500, content={"reachable": False, "reason": str(exc)})

        @app.post("/conversations/status")
        async def dashboard_conversation_statuses(request: Request):
            body = await request.json()
            raw_ids = body.get("session_ids") if isinstance(body, dict) else []
            session_ids = [
                _safe_session_id(value)
                for value in (raw_ids if isinstance(raw_ids, list) else [])[:500]
            ]
            try:
                store = SessionStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                statuses = await asyncio.to_thread(
                    store.conversation_statuses, session_ids
                )
                return {
                    "reachable": True,
                    "statuses": statuses,
                }
            except Exception as exc:  # noqa: BLE001
                return {"reachable": False, "statuses": {}, "reason": str(exc)}

        @app.post("/conversations/judge-backfill")
        async def dashboard_conversation_judge_backfill(request: Request):
            """Sweep archived sessions without a quality score into the async
            judge queue (admin only). Fills the historical gap for sessions the
            value filter skipped before post-ingest judging existed."""
            user = _session_user(request)
            if user is None or str(user.get("role") or "user") != "admin":
                raise HTTPException(status_code=403, detail="admin required")
            judge_queue = getattr(owner, "_session_judge_queue", None)
            if judge_queue is None or not getattr(judge_queue, "_started", False):
                raise HTTPException(status_code=503, detail="judge worker unavailable")
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            body = body if isinstance(body, dict) else {}
            want_status = str(body.get("status") or "").strip().lower()
            try:
                limit = max(1, min(500, int(body.get("limit") or 200)))
            except (TypeError, ValueError):
                limit = 200
            store = SessionStore.from_config(
                _tenant_effective_config(owner), tenant_id=current_tenant_id()
            )
            rows = await asyncio.to_thread(store.list_conversations, limit=100000)
            tenant_id = current_tenant_id()
            enqueued: list[str] = []
            for row in rows:
                sid = str(row.get("session_id") or "").strip()
                if not sid:
                    continue
                if want_status and str(row.get("status") or "") != want_status:
                    continue
                judge = row.get("judge") if isinstance(row.get("judge"), dict) else {}
                if isinstance(judge.get("overall_score"), (int, float)):
                    continue
                if judge_queue.pending(tenant_id, sid):
                    continue
                if judge_queue.enqueue(tenant_id, sid):
                    enqueued.append(sid)
                if len(enqueued) >= limit:
                    break
            return {
                "reachable": True,
                "enqueued": len(enqueued),
                "backlog": judge_queue.backlog(),
                "session_ids": enqueued[:limit],
            }

        @app.get("/conversations/{session_id}")
        async def dashboard_conversation_detail(session_id: str):
            try:
                store = SessionStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                session = await asyncio.to_thread(
                    store.load_session, _safe_session_id(session_id)
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
            return _session_detail_payload(session)

        @app.get("/conversations/{session_id}/process")
        async def dashboard_conversation_process(session_id: str):
            cycles = _history_cycles(
                _tenant_effective_config(owner),
                limit=50,
                session_id=_safe_session_id(session_id),
            )
            return {"cycles": cycles}

        @app.get("/history")
        async def dashboard_history(limit: int = 50, session_id: str = ""):
            return {
                "cycles": _history_cycles(
                    _tenant_effective_config(owner),
                    limit=max(1, int(limit or 50)),
                    session_id=_safe_session_id(session_id) if session_id else "",
                )
            }

        @app.get("/api/skill-experiences")
        async def api_skill_experiences(
            limit: int = 50,
            offset: int = 0,
            kind: str = "",
            skill: str = "",
            search: str = "",
            refresh: bool = False,
        ):
            safe_limit = min(200, max(1, int(limit or 50)))
            safe_offset = max(0, int(offset or 0))
            wanted_kind = str(kind or "").strip().lower()
            if wanted_kind not in {"", "defect", "exemplary"}:
                raise HTTPException(status_code=400, detail="invalid experience kind")
            cache_key = (
                f"skill-experiences:{id(owner.config)}:"
                f"{wanted_kind}:{skill.strip()}:{search.strip().lower()}"
            )
            if refresh:
                _invalidate_dashboard_cache("skill-experiences:")

            def load_experiences() -> dict[str, Any]:
                store = _build_experience_library_store(
                    _tenant_effective_config(owner)
                )
                store.backfill_legacy_evidence()
                return store.list_experiences(
                    kind=wanted_kind,
                    skill=skill,
                    search=search,
                )

            try:
                payload = await asyncio.to_thread(
                    _cached_dashboard_value,
                    cache_key,
                    15.0,
                    load_experiences,
                )
                rows = payload.get("items") or []
                page = rows[safe_offset : safe_offset + safe_limit]
                return {
                    "items": page,
                    "stats": payload.get("stats") or {},
                    "skill_counts": payload.get("skill_counts") or {},
                    "total": len(rows),
                    "limit": safe_limit,
                    "offset": safe_offset,
                    "has_more": safe_offset + len(page) < len(rows),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ExperienceLibrary] list failed: %s", exc)
                raise HTTPException(status_code=503, detail=f"经验库暂不可用：{exc}") from exc

        @app.get("/api/skill-evolution/session-analysis/audit")
        async def api_session_filter_audit(limit: int = 100, decision: str = ""):
            safe_limit = max(1, int(limit or 100))
            wanted = str(decision or "").strip().lower()
            cache_key = f"session-filter-audit:{id(owner.config)}:{safe_limit}:{wanted}"

            def load_audit() -> dict[str, Any]:
                store = SessionStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                return {
                    "stats": store.filter_stats(),
                    "items": store.list_filter_audit(
                        limit=safe_limit,
                        decision=wanted,
                    ),
                }

            try:
                return await asyncio.to_thread(
                    lambda: _cached_dashboard_value(
                        cache_key,
                        30.0,
                        load_audit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "stats": {
                        "total": 0,
                        "decisions": {},
                        "statuses": {},
                        "modes": {},
                    },
                    "items": [],
                    "reason": str(exc),
                }

        @app.get("/api/mined-skills")
        async def api_mined_skills():
            try:
                store = ValidationStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                registered = {
                    str(skill.get("name") or "")
                    for skill in (
                        owner.skill_manager.get_all_skills()
                        if owner.skill_manager is not None
                        else []
                    )
                }
                skills = list_mined_skill_statuses(
                    store,
                    registered_skill_names=registered,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"候选存储不可用：{exc}",
                ) from exc
            return {
                "skills": skills,
                "external_runtime_required": False,
            }

        @app.post("/api/mined-skills/{skill_name}/submit")
        async def api_submit_mined_skill(skill_name: str, request: Request):
            user = _session_user(request) or {}
            current_skill = None
            if owner.skill_manager is not None:
                current_skill = next(
                    (
                        skill
                        for skill in owner.skill_manager.get_all_skills()
                        if str(skill.get("name") or "") == skill_name
                    ),
                    None,
                )
            try:
                store = ValidationStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                submitted = submit_mined_skill(
                    store,
                    skill_name,
                    current_skill=current_skill,
                    submitted_by=str(
                        user.get("id") or user.get("username") or ""
                    ),
                )
            except MiningLifecycleError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"提交候选失败：{exc}",
                ) from exc
            job = submitted["job"]
            decision = (
                submitted.get("decision")
                if isinstance(submitted.get("decision"), dict)
                else None
            )
            return {
                "created": bool(submitted.get("created")),
                "job_id": str(job.get("job_id") or ""),
                "skill_name": str(job.get("skill_name") or skill_name),
                "status": (
                    str(decision.get("status") or "candidate")
                    if decision
                    else "candidate"
                ),
                "dataset_format": (job.get("source") or {}).get(
                    "dataset_format"
                ),
                "question_count": (job.get("source") or {}).get(
                    "question_count"
                ),
            }

        @app.post("/api/mined-jobs/{job_id}/skills/{skill_name}/submit")
        async def api_submit_mined_job_skill(
            job_id: str,
            skill_name: str,
            request: Request,
        ):
            """Send a completed task's edited bundle to the evolution review gate."""
            user = _session_user(request) or {}
            current_skill = None
            if owner.skill_manager is not None:
                current_skill = next(
                    (
                        skill
                        for skill in owner.skill_manager.get_all_skills()
                        if str(skill.get("name") or "") == skill_name
                    ),
                    None,
                )
            try:
                payload = {}
                try:
                    parsed_payload = await request.json()
                    if isinstance(parsed_payload, dict):
                        payload = parsed_payload
                except (json.JSONDecodeError, ValueError):
                    pass
                workspace = resolve_mined_job_skill_root(
                    job_id,
                    skill_name,
                    artifact_path=str(payload.get("artifact_path") or ""),
                )
                store = ValidationStore.from_config(
                    _tenant_effective_config(owner), tenant_id=current_tenant_id()
                )
                submitted = submit_mined_skill(
                    store,
                    skill_name,
                    current_skill=current_skill,
                    submitted_by=str(
                        user.get("id") or user.get("username") or ""
                    ),
                    skillminer_root=workspace,
                    mining_job_id=job_id,
                )
            except MiningLifecycleError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"提交候选失败：{exc}",
                ) from exc
            candidate = submitted["job"]
            decision = (
                submitted.get("decision")
                if isinstance(submitted.get("decision"), dict)
                else None
            )
            return {
                "created": bool(submitted.get("created")),
                "job_id": str(candidate.get("job_id") or ""),
                "mining_job_id": job_id,
                "skill_name": str(candidate.get("skill_name") or skill_name),
                "status": (
                    str(decision.get("status") or "candidate")
                    if decision
                    else "candidate"
                ),
                "dataset_format": (candidate.get("source") or {}).get(
                    "dataset_format"
                ),
                "question_count": (candidate.get("source") or {}).get(
                    "question_count"
                ),
            }

        def _validation_store() -> ValidationStore:
            return ValidationStore.from_config(
                _tenant_effective_config(owner), tenant_id=current_tenant_id()
            )

        def _candidate_feedback_store() -> CandidateFeedbackStore:
            return CandidateFeedbackStore.from_config(
                _tenant_effective_config(owner), tenant_id=current_tenant_id()
            )

        async def _candidate_request_user(request: Request) -> dict[str, Any] | None:
            if getattr(request.state, "service_root_authenticated", False):
                return {
                    "id": "service-root",
                    "display_name": "Service Root",
                    "role": "admin",
                }
            return (
                getattr(request.state, "console_user", None)
                or await asyncio.to_thread(_session_user, request)
            )

        def _candidate_feedback_actor(user: dict[str, Any]) -> tuple[str, str]:
            actor_id = str(user.get("id") or user.get("username") or "admin")
            actor_name = str(
                user.get("display_name")
                or user.get("username")
                or user.get("id")
                or "admin"
            )
            return actor_id, actor_name

        def _record_candidate_action_feedback(
            job: dict[str, Any],
            user: dict[str, Any],
            *,
            adopted: bool,
            rejected: bool,
        ) -> dict[str, Any]:
            actor_id, actor_name = _candidate_feedback_actor(user)
            return _candidate_feedback_store().update(
                str(job.get("job_id") or ""),
                {"adopted": adopted, "rejected": rejected},
                actor_id=actor_id,
                actor_name=actor_name,
                candidate_revision=max(
                    1, int(job.get("candidate_revision") or 1)
                ),
            )

        def _with_candidate_feedback(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not items:
                return []
            store = _candidate_feedback_store()
            return [{**item, "feedback": store.load(item["job_id"])} for item in items]

        def _validation_candidate_detail_payload(store: ValidationStore, job_id: str) -> dict[str, Any]:
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            evaluation = store.load_evaluation(job_id)
            decision = store.load_decision(job_id)
            return {
                **_candidate_payload(job, evaluation, decision),
                **_skill_diff_payload(job, owner.config),
                "feedback": _candidate_feedback_store().load(job_id),
            }

        @app.get("/api/skill-candidates")
        async def api_validation_candidates(
            scope: str = "open",
            limit: int = 20,
            offset: int = 0,
            refresh: bool = False,
            compact: bool = False,
        ):
            safe_limit = min(200, max(1, int(limit or 20)))
            safe_offset = max(0, int(offset or 0))
            try:
                store = _validation_store()
                cache_key = f"candidates:{id(owner.config)}:{current_tenant_id()}:{scope}"
                if refresh:
                    _invalidate_dashboard_cache(cache_key)
                candidates = await asyncio.to_thread(
                    _cached_dashboard_value,
                    cache_key,
                    15.0,
                    lambda: _candidate_list_payloads(
                        store,
                        scope=scope,
                        user_alias=str(owner.config.sharing_user_alias or ""),
                    ),
                )
            except Exception:
                candidates = []
            page = candidates[safe_offset : safe_offset + safe_limit]
            # Human feedback is read fresh, outside the replay/list cache.
            page = await asyncio.to_thread(_with_candidate_feedback, page)
            if compact:
                page = [_compact_candidate_payload(item) for item in page]
            return {
                "candidates": page,
                "total": len(candidates),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": safe_offset + len(page) < len(candidates),
            }

        @app.get("/api/skill-candidates/{job_id}/detail")
        async def api_validation_candidate_detail(job_id: str):
            return await asyncio.to_thread(
                lambda: _validation_candidate_detail_payload(_validation_store(), job_id)
            )

        @app.patch("/api/skill-candidates/{job_id}/feedback")
        async def api_validation_candidate_feedback(job_id: str, body: dict[str, Any], request: Request):
            user = await _candidate_request_user(request)
            _require_admin_user(user)
            if set(body) != {"reviewed"} or type(body.get("reviewed")) is not bool:
                raise HTTPException(
                    status_code=400,
                    detail="only reviewed can be marked manually",
                )

            def update_feedback():
                store = _validation_store()
                job = store.load_job(job_id)
                if not job:
                    raise HTTPException(status_code=404, detail="candidate not found")
                if store.load_decision(job_id):
                    raise HTTPException(status_code=409, detail="已处理候选的标记只读")
                actor_id, actor_name = _candidate_feedback_actor(user)
                return _candidate_feedback_store().update(
                    job_id, body,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    candidate_revision=max(1, int(job.get("candidate_revision") or 1)),
                )

            try:
                feedback = await asyncio.to_thread(update_feedback)
            except FeedbackConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"job_id": job_id, "feedback": feedback}

        @app.post("/api/skill-candidates/{job_id}/evaluate")
        async def api_validation_candidate_evaluate(job_id: str, refresh: bool = False):
            store = _validation_store()
            job = store.load_job(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            cached = None if refresh else store.load_fresh_evaluation(job_id, job)
            if cached:
                return {**_evaluation_payload(job, cached, cached=True), **_skill_diff_payload(job, owner.config)}
            result = await _evaluate_candidate_job(owner.config, owner, job)
            store.save_evaluation(job_id, result)
            _invalidate_dashboard_cache(f"candidates:{id(owner.config)}")
            return {**_evaluation_payload(job, result, cached=False), **_skill_diff_payload(job, owner.config)}

        @app.post("/api/skill-candidates/{job_id}/validate")
        async def api_validation_candidate_validate(job_id: str, request: Request):
            user = await _candidate_request_user(request)
            _require_admin_user(user)
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            mode = str(body.get("mode") or "auto")
            store = _validation_store()
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            current_revision = max(1, int(job.get("candidate_revision") or 1))
            requested_revision = body.get("candidate_revision")
            if requested_revision is not None:
                try:
                    requested_revision = int(requested_revision)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail="candidate_revision must be an integer",
                    ) from exc
                if requested_revision != current_revision:
                    raise HTTPException(
                        status_code=409,
                        detail="candidate revision changed; reload before publishing",
                    )
            existing_decision = store.load_decision(job_id)
            if existing_decision:
                if existing_decision.get("status") != "published":
                    raise HTTPException(status_code=409, detail="candidate already processed")
                feedback = _record_candidate_action_feedback(
                    job, user, adopted=True, rejected=False
                )
                return {**existing_decision, "feedback": feedback}
            evaluation = store.load_fresh_evaluation(job_id, job)
            if not evaluation:
                evaluation = await _evaluate_candidate_job(owner.config, owner, job)
                store.save_evaluation(job_id, evaluation)
            accepted = bool(evaluation.get("accepted"))
            if mode != "force" and not accepted:
                decision = {
                    "status": "rejected",
                    "accepted": False,
                    "reason": evaluation.get("reason") or "evaluation did not pass",
                    "evaluation": evaluation,
                }
                store.save_decision(job_id, decision)
                _invalidate_dashboard_cache(f"candidates:{id(owner.config)}")
                return decision
            candidate_skill = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else None
            if not candidate_skill or not candidate_skill.get("name"):
                raise HTTPException(status_code=400, detail="candidate missing skill payload")

            name = str(candidate_skill.get("name") or "")
            current = job.get("current_skill") if isinstance(job.get("current_skill"), dict) else None
            created = current is None
            try:
                bundle = candidate_skill_bundle(candidate_skill)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "validation_failed",
                        "instance_id": _INSTANCE_ID,
                        "tenant_id": current_tenant_id(),
                        "job_id": job_id,
                        "candidate_revision": current_revision,
                        "skill_name": name,
                        "reason": str(exc),
                    },
                ) from exc
            effective = _tenant_effective_config(owner)
            tenant_id = current_tenant_id()
            mutation_id = (
                f"candidate:{tenant_id}:{job_id}:r{current_revision}"
            )
            try:
                commit = await asyncio.to_thread(
                    SkillMutationService.from_config(
                        effective,
                        tenant_id=tenant_id,
                    ).publish_bundle,
                    action="publish" if created else "update",
                    name=name,
                    mutation_id=mutation_id,
                    bundle=bundle,
                    tenant_ids=[tenant_id],
                    metadata={
                        "source": "candidate-review",
                        "job_id": job_id,
                        "candidate_revision": current_revision,
                    },
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "status": "validation_failed",
                        "instance_id": _INSTANCE_ID,
                        "tenant_id": tenant_id,
                        "job_id": job_id,
                        "candidate_revision": current_revision,
                        "mutation_id": mutation_id,
                        "skill_name": name,
                        "reason": str(exc),
                    },
                ) from exc
            except Exception as exc:
                logger.warning(
                    "[CandidatePublish] durable publish failed instance=%s tenant=%s "
                    "job=%s revision=%s mutation=%s skill=%s: %s",
                    _INSTANCE_ID,
                    tenant_id,
                    job_id,
                    current_revision,
                    mutation_id,
                    name,
                    exc,
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "status": "publish_failed",
                        "retryable": True,
                        "instance_id": _INSTANCE_ID,
                        "tenant_id": tenant_id,
                        "job_id": job_id,
                        "candidate_revision": current_revision,
                        "mutation_id": mutation_id,
                        "skill_name": name,
                        "reason": str(exc),
                    },
                ) from exc

            local_cache = {"synced": True, "reason": ""}
            try:
                write_skill_bundle(
                    os.path.join(owner._skills_dir(), name),
                    bundle,
                    clean=True,
                )
                loaded = owner._reload_skill_manager()
            except Exception as exc:  # durable PG publication already succeeded
                loaded = 0
                local_cache = {"synced": False, "reason": str(exc)}
                logger.warning(
                    "[CandidatePublish] local cache refresh failed instance=%s "
                    "tenant=%s job=%s revision=%s mutation=%s skill=%s: %s",
                    _INSTANCE_ID,
                    tenant_id,
                    job_id,
                    current_revision,
                    mutation_id,
                    name,
                    exc,
                )
            expected = dict(commit.get("expected") or {})
            event_id = str(commit.get("event_id") or "")
            cloud = {
                "synced": True,
                "action": "publish",
                "event_id": event_id,
                "record": expected,
            }
            decision = {
                "status": "published",
                "accepted": True,
                "instance_id": _INSTANCE_ID,
                "tenant_id": tenant_id,
                "job_id": job_id,
                "candidate_revision": current_revision,
                "mutation_id": mutation_id,
                "event_id": event_id,
                "skill_name": name,
                "created": created,
                "version": int(expected.get("version") or 0),
                "tree_sha256": str(expected.get("tree_sha256") or ""),
                "loaded_skills": loaded,
                "local_cache": local_cache,
                "cloud": cloud,
                "evaluation": evaluation,
            }
            store.save_decision(job_id, decision)
            feedback = _record_candidate_action_feedback(
                job, user, adopted=True, rejected=False
            )
            _invalidate_dashboard_cache(
                f"candidates:{id(owner.config)}",
                f"status:{id(owner.config)}",
            )
            return {**decision, "feedback": feedback}

        @app.put("/api/skill-candidates/{job_id}/content")
        async def api_validation_candidate_update_content(job_id: str, request: Request):
            """Human review edit: revise the candidate skill before publishing.

            Bumps ``candidate_revision`` so the cached True Replay evaluation
            computed against the previous content is treated as stale and the
            candidate must be re-evaluated before it can be published again.
            """
            _require_admin_user(getattr(request.state, "console_user", None) or _session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="invalid request body")
            store = _validation_store()
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            candidate_skill = (
                job.get("candidate_skill")
                if isinstance(job.get("candidate_skill"), dict)
                else {}
            )
            for key in ("name", "description", "category", "content"):
                if key in body and body[key] is not None:
                    candidate_skill[key] = str(body[key])
            if not str(candidate_skill.get("name") or "").strip():
                raise HTTPException(status_code=400, detail="candidate skill name is required")
            job["candidate_skill"] = candidate_skill
            job["candidate_revision"] = max(1, int(job.get("candidate_revision") or 1)) + 1
            job["updated_at"] = _utc_now_iso()
            # Drop artifacts bound to the previous revision (cached evaluation,
            # per-user results, rendered candidate bundle); save_job rewrites
            # the bundle from the edited candidate_skill.
            store.reset_job_artifacts(job_id)
            store.save_job(job)
            _invalidate_dashboard_cache(
                f"candidates:{id(owner.config)}",
                f"status:{id(owner.config)}",
            )
            return await asyncio.to_thread(_validation_candidate_detail_payload, store, job_id)

        async def _reject_validation_candidate(
            job_id: str, request: Request
        ) -> dict[str, Any]:
            user = await _candidate_request_user(request)
            _require_admin_user(user)
            store = _validation_store()
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            existing_decision = store.load_decision(job_id)
            if existing_decision and existing_decision.get("status") != "rejected":
                raise HTTPException(status_code=409, detail="candidate already processed")
            if existing_decision:
                decision = existing_decision
            else:
                _actor_id, actor_name = _candidate_feedback_actor(user)
                decision = {
                    "status": "rejected",
                    "accepted": False,
                    "reason": "用户手动驳回",
                    "mode": "manual",
                    "reviewer": actor_name,
                }
                store.save_decision(job_id, decision)
                decision = store.load_decision(job_id) or decision
            feedback = _record_candidate_action_feedback(
                job, user, adopted=False, rejected=True
            )
            _invalidate_dashboard_cache(f"candidates:{id(owner.config)}")
            return {**decision, "feedback": feedback}

        @app.post("/api/skill-candidates/{job_id}/reject")
        async def api_validation_candidate_reject(job_id: str, request: Request):
            return await _reject_validation_candidate(job_id, request)

        @app.delete("/api/skill-candidates/{job_id}")
        async def api_validation_candidate_delete(job_id: str, request: Request):
            """Compatibility alias: deleting a candidate now records a rejection."""
            return await _reject_validation_candidate(job_id, request)

        @app.post("/internal/reload-skills")
        async def reload_skills(
            request: Request,
        ):
            owner = request.app.state.owner
            await owner._pull_skills_from_cloud()
            skill_count = len(owner.skill_manager.get_all_skills()) if owner.skill_manager else 0
            return {"ok": True, "skills": skill_count}

        from ..logging_http import RequestLogMiddleware, install_logging_status
        from ..logging_runtime import event

        install_logging_status(app, _require_admin_user)
        from .experience_sync import install as install_experience_sync

        install_experience_sync(app, owner, get_tenant_registry, _require_admin_user)
        app.add_middleware(RequestLogMiddleware)
        from team_ontology.config import OntologyConfig

        ontology_enabled = OntologyConfig.from_host(app.state.owner.config).enabled
        event(logger, "ontology.configuration", enabled=ontology_enabled,
              routes_registered=False, schema=app.state.owner.config.storage_pg_schema,
              state_dir=OntologyConfig.from_host(app.state.owner.config).state_dir)
        if ontology_enabled:
            from team_ontology.api import install_native

            install_native(app)
            event(logger, "ontology.routes_registered", routes_registered=True)
        return app
