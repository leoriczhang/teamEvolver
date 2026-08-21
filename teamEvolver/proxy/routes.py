"""FastAPI application and route wiring for the teamEvolver service.

``RoutesMixin`` builds the ``FastAPI`` app and its endpoints (console,
health, skill/user admin, model settings, and internal skill reload). Route bodies delegate to the owning
:class:`~teamEvolver.proxy.server.ProxyServer` instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    LOCAL_OPENVIKING_ENDPOINT,
    VOLCENGINE_OPENVIKING_ENDPOINT,
    resolve_viking_endpoint,
)
from ..config_store import ConfigStore
from ..integrations.agent_protocol import (
    AgentProtocolError,
    is_v1_payload,
    normalize_session_envelope,
)
from ..integrations.agent_registry import (
    issue_agent_access_token,
    list_agents,
    public_agent_record,
    register_agent,
    verify_agent_access_token,
)
from ..integrations.context_workspace import verify_context_usage
from ..mining_lifecycle import (
    MiningLifecycleError,
    list_mined_skill_statuses,
    resolve_mined_job_skill_root,
    submit_mined_skill,
)
from ..progressive_replay import (
    aggregate_case_checklists,
    progressive_replay_decision,
    select_replay_cases,
)
from ..session_filter import SessionValueClassifier
from ..session_store import SessionStore
from ..skills import frontmatter
from ..skills.hub import SkillHub
from ..skills.mutations import SkillMutationService
from ..skills.render import build_skill_md
from ..storage import is_not_found_error
from ..validation.store import ValidationStore
from ..validation.worker import ValidationWorker
from .users_admin import (
    _find_user,
    _load_registry,
    _public_user,
    _registry_path,
    _save_registry,
    _upsert_user,
    _verify_password,
    resolve_agent_subject_user_id,
    resolve_registered_user_id,
    sync_agent_subject_mappings,
    sync_openviking_user,
)

logger = logging.getLogger(__name__)
_SESSION_COOKIE = "teamEvolver_console_session"
_SESSION_TTL_SECONDS = 24 * 60 * 60
_DASHBOARD_CACHE: dict[str, tuple[float, Any]] = {}


def _cached_dashboard_value(key: str, ttl_seconds: float, loader):
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
    for key in list(_DASHBOARD_CACHE):
        if any(key.startswith(prefix) for prefix in prefixes):
            _DASHBOARD_CACHE.pop(key, None)


def _model_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    llm = store_data.get("llm") if isinstance(store_data.get("llm"), dict) else {}
    api_key = str(getattr(config, "llm_api_key", "") or llm.get("api_key") or "")
    temperature = (
        getattr(config, "llm_temperature", 0.0)
        if getattr(config, "llm_temperature", None) is not None
        else llm.get("temperature", 0.4)
    )
    return {
        "provider": str(llm.get("provider") or getattr(config, "llm_provider", "") or "custom"),
        "base_url": str(getattr(config, "llm_api_base", "") or llm.get("api_base") or ""),
        "model": str(getattr(config, "llm_model_id", "") or llm.get("model_id") or ""),
        "max_tokens": int(getattr(config, "llm_max_tokens", 0) or llm.get("max_tokens") or 100000),
        "temperature": float(temperature),
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
    dreamcycle_key = str(
        dreamcycle.get("llm_api_key")
        or getattr(config, "dreamcycle_llm_api_key", "")
        or ""
    )
    dreamcycle_embed_key = str(
        dreamcycle.get("embed_api_key")
        or getattr(config, "dreamcycle_embed_api_key", "")
        or dreamcycle_key
        or ""
    )
    from ..integrations.dreamcycle_runtime import available_jobs

    dreamcycle_job_prompts = (
        dreamcycle.get("job_prompts")
        if isinstance(dreamcycle.get("job_prompts"), dict)
        else {}
    )
    dreamcycle_job_settings = (
        dreamcycle.get("job_settings")
        if isinstance(dreamcycle.get("job_settings"), dict)
        else {}
    )
    default_job_runtime = {
        "model": "",
        "base_url": "",
        "temperature": float(
            dreamcycle.get("temperature", 0.3)
            if dreamcycle.get("temperature") is not None
            else 0.3
        ),
        "max_tokens": int(
            dreamcycle.get("llm_max_tokens", 4096) or 4096
        ),
        "max_turns": int(
            dreamcycle.get("max_turns_per_job", 25) or 25
        ),
        "max_errors": int(
            dreamcycle.get("max_consecutive_errors", 3) or 3
        ),
    }
    raw_enabled_jobs = dreamcycle.get(
        "enabled_jobs",
        [
            "team_overview",
            "deduplication",
            "cleanup",
            "onboarding_check",
            "consolidate",
        ],
    )
    enabled_jobs = {
        str(item).strip()
        for item in (
            raw_enabled_jobs
            if isinstance(raw_enabled_jobs, (list, tuple, set))
            else str(raw_enabled_jobs or "").replace("\n", ",").split(",")
        )
        if str(item).strip()
    }
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
            "model": str(
                dreamcycle.get("llm_model")
                or getattr(config, "dreamcycle_llm_model", "")
                or ""
            ),
            "base_url": str(
                dreamcycle.get("llm_base_url")
                or getattr(config, "dreamcycle_llm_base_url", "")
                or ""
            ),
            "api_key_present": bool(dreamcycle_key),
            "engine": "teamEvolver-native-dreamcycle",
            "full_capabilities": True,
            "llm_max_tokens": int(
                dreamcycle.get("llm_max_tokens", 4096) or 4096
            ),
            "temperature": float(
                dreamcycle.get("temperature", 0.3)
                if dreamcycle.get("temperature") is not None
                else 0.3
            ),
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
            "embed_model": str(dreamcycle.get("embed_model") or ""),
            "embed_base_url": str(dreamcycle.get("embed_base_url") or ""),
            "embed_api_key_present": bool(dreamcycle_embed_key),
            "semantic_dedup_enabled": bool(
                str(dreamcycle.get("embed_model") or "").strip()
                and dreamcycle_embed_key
            ),
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
            "scheduler": {
                "active_start_hour": int(
                    dreamcycle.get("active_start_hour", 0) or 0
                ),
                "active_end_hour": int(
                    dreamcycle.get("active_end_hour", 6)
                    if dreamcycle.get("active_end_hour") is not None
                    else 6
                ),
                "rounds_per_window": int(
                    dreamcycle.get("rounds_per_window", 3) or 3
                ),
                "round_interval_minutes": int(
                    dreamcycle.get("round_interval_minutes", 90) or 90
                ),
                "max_turns_per_job": int(
                    dreamcycle.get("max_turns_per_job", 25) or 25
                ),
                "max_consecutive_errors": int(
                    dreamcycle.get("max_consecutive_errors", 3) or 3
                ),
                "retry_delay_seconds": int(
                    dreamcycle.get("retry_delay_seconds", 300) or 300
                ),
            },
            "jobs": [
                {
                    **job,
                    "enabled": job["id"] in enabled_jobs,
                    "effective_prompt": str(
                        dreamcycle_job_prompts.get(job["id"])
                        or job["default_prompt"]
                    ),
                    "overridden": bool(
                        dreamcycle_job_prompts.get(job["id"])
                    ),
                    "runtime": {
                        **default_job_runtime,
                        **(
                            dreamcycle_job_settings.get(job["id"])
                            if isinstance(
                                dreamcycle_job_settings.get(job["id"]),
                                dict,
                            )
                            else {}
                        ),
                    },
                    "default_runtime": default_job_runtime,
                    "settings_overridden": bool(
                        dreamcycle_job_settings.get(job["id"])
                    ),
                }
                for job in available_jobs(
                    str(getattr(config, "team_display_name", "") or "Team")
                )
            ],
        },
    }


def _langfuse_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    """Snapshot of the persisted Langfuse settings for the console form.

    Secret keys are never echoed back; only presence flags are exposed, mirroring
    how ``_model_settings_payload`` handles the model API key.
    """
    langfuse = store_data.get("langfuse") if isinstance(store_data.get("langfuse"), dict) else {}
    from ..observability import langfuse_status

    tracing_status = langfuse_status()

    def _as_list(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        if value in (None, ""):
            return []
        return [item for raw in str(value).replace("\n", ",").split(",") if (item := raw.strip())]

    return {
        "enabled": bool(langfuse.get("enabled", getattr(config, "langfuse_enabled", False))),
        "host": str(langfuse.get("host") or getattr(config, "langfuse_host", "") or "https://cloud.langfuse.com"),
        "public_key": str(langfuse.get("public_key") or getattr(config, "langfuse_public_key", "") or ""),
        "public_key_present": bool(langfuse.get("public_key") or getattr(config, "langfuse_public_key", "")),
        "secret_key_present": bool(langfuse.get("secret_key") or getattr(config, "langfuse_secret_key", "")),
        "tracing_enabled": bool(
            langfuse.get(
                "tracing_enabled",
                getattr(config, "langfuse_tracing_enabled", False),
            )
        ),
        "tracing_environment": str(
            langfuse.get("tracing_environment")
            or getattr(config, "langfuse_tracing_environment", "")
            or "local"
        ),
        "tracing_release": str(
            langfuse.get("tracing_release")
            or getattr(config, "langfuse_tracing_release", "")
            or ""
        ),
        "tracing_sample_rate": float(
            langfuse.get(
                "tracing_sample_rate",
                getattr(config, "langfuse_tracing_sample_rate", 1.0),
            )
        ),
        "tracing_capture_content": bool(
            langfuse.get(
                "tracing_capture_content",
                getattr(config, "langfuse_tracing_capture_content", True),
            )
        ),
        "tracing_flush_at": int(
            langfuse.get(
                "tracing_flush_at",
                getattr(config, "langfuse_tracing_flush_at", 1),
            )
        ),
        "tracing_flush_interval_seconds": float(
            langfuse.get(
                "tracing_flush_interval_seconds",
                getattr(
                    config,
                    "langfuse_tracing_flush_interval_seconds",
                    1.0,
                ),
            )
        ),
        "tracing_status": tracing_status,
        "max_sessions": int(langfuse.get("max_sessions") or getattr(config, "langfuse_max_sessions", 100) or 100),
        "page_limit": int(langfuse.get("page_limit") or getattr(config, "langfuse_page_limit", 50) or 50),
        "timeout_seconds": int(
            langfuse.get("timeout_seconds") or getattr(config, "langfuse_timeout_seconds", 30) or 30
        ),
        "default_environment": _as_list(
            langfuse.get("default_environment", getattr(config, "langfuse_default_environment", []))
        ),
        "default_user_id": str(
            langfuse.get("default_user_id") or getattr(config, "langfuse_default_user_id", "") or ""
        ),
        "default_tags": _as_list(
            langfuse.get("default_tags", getattr(config, "langfuse_default_tags", []))
        ),
        "default_release": str(
            langfuse.get("default_release") or getattr(config, "langfuse_default_release", "") or ""
        ),
        "default_version": str(
            langfuse.get("default_version") or getattr(config, "langfuse_default_version", "") or ""
        ),
        "default_trace_name": str(
            langfuse.get("default_trace_name") or getattr(config, "langfuse_default_trace_name", "") or ""
        ),
        "mapper_enabled": bool(
            langfuse.get("mapper_enabled", getattr(config, "langfuse_mapper_enabled", False))
        ),
        # The mapper code is operator-authored and meant to be viewed/edited in
        # the console, so (unlike secrets) it is echoed back verbatim.
        "mapper_code": str(
            langfuse.get("mapper_code")
            if langfuse.get("mapper_code") is not None
            else getattr(config, "langfuse_mapper_code", "") or ""
        ),
    }


def _require_admin_user(user: dict | None) -> None:
    if not user or str(user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")


def _console_sessions_path(config) -> Path:
    """Persist console login sessions next to the users registry so a service
    restart does not force every logged-in operator back to the login page."""
    return _registry_path(config).parent / "console_sessions.json"


def _load_console_sessions(config) -> dict[str, dict]:
    path = _console_sessions_path(config)
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


def _max_session_body_bytes() -> int:
    try:
        value = int(os.environ.get("TEAMEVOLVER_MAX_SESSION_BODY_BYTES", str(32 * 1024 * 1024)) or 0)
    except ValueError:
        value = 32 * 1024 * 1024
    return max(1024, value)


async def _read_limited_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    limit = _max_session_body_bytes()
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"session body exceeds {limit} bytes")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="session body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="session body must be an object")
    return parsed


def _session_queue_snapshot(config, *, limit: int = 100) -> dict[str, Any]:
    try:
        store = SessionStore.from_config(config)
        rows = store.list_queue(limit=limit if limit > 0 else 100000)
        return {
            "reachable": True,
            "pending": len(rows),
            "sessions": rows[:limit] if limit > 0 else [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "sessions": [], "pending": 0, "reason": str(exc)}


def _session_detail_payload(session: dict[str, Any]) -> dict[str, Any]:
    status = str(session.get("status") or "queued")
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    return {
        "meta": {
            "title": session.get("title") or "",
            "user_alias": session.get("user_alias") or "",
            "status": status,
            "num_turns": len(turns) if turns else metrics.get("interaction_turns"),
        },
        "turns_available": bool(turns),
        "turns_source": "archive",
        "system_prompt": session.get("system_prompt") or "",
        "injected_skills": session.get("injected_skills") or [],
        "used_skills": session.get("used_skills") or [],
        "metrics": metrics,
        "turns": turns,
        "value_judge": session.get("value_judge") if isinstance(session.get("value_judge"), dict) else {},
    }


def _history_from_archived_sessions(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    try:
        store = SessionStore.from_config(config)
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
                "timestamp": row.get("ingested_at") or row.get("timestamp"),
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


def _history_from_evolve_file(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    capped = max(1, int(limit or 50))
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
                    rows.append(_filter_cycle_for_session(record, session_id))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("[History] failed to read evolve history %s: %s", path, exc)
            continue
        rows.reverse()
        return rows[:capped]
    return []


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
    metric_keys = ("interaction_turns", "tool_call_count", "total_tokens")
    totals: dict[str, dict[str, int]] = {
        key: {"baseline": 0, "candidate": 0} for key in metric_keys
    }
    found = False
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
            totals[key]["baseline"] += int(metric.get("baseline") or 0)
            totals[key]["candidate"] += int(metric.get("candidate") or 0)
            found = True
    if not found:
        return {}
    dimensions: dict[str, dict[str, Any]] = {}
    for key in metric_keys:
        baseline_value = totals[key]["baseline"]
        candidate_value = totals[key]["candidate"]
        delta = baseline_value - candidate_value
        dimensions[key] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "winner": "candidate" if delta > 0 else ("baseline" if delta < 0 else "tie"),
        }
    return dimensions


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
        from ..true_replay import evaluate_job

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
        selected = select_replay_cases(job.get("replay_cases") or [])
        window_results = []
        for window, case_index in selected:
            result = await asyncio.to_thread(
                evaluate_job,
                job_id,
                job=job,
                case_index=case_index,
                timeout=replay_timeout,
                max_interactions=max_interactions,
            )
            window_results.append((window, result))
        replay = ValidationWorker._aggregate_true_replay_windows(
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
    name = str(skill_name or "").strip()
    if not name:
        return ""
    for raw_root in [getattr(config, "skills_dir", ""), os.path.abspath("skills")]:
        root = str(raw_root or "").strip()
        if not root:
            continue
        direct = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(direct):
            try:
                with open(direct, "r", encoding="utf-8") as handle:
                    return handle.read()
            except Exception:
                pass
        try:
            for current_root, _, files in os.walk(root):
                if "SKILL.md" not in files:
                    continue
                path = os.path.join(current_root, "SKILL.md")
                try:
                    parsed = frontmatter.parse_skill_md(path)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict) and str(parsed.get("name") or os.path.basename(current_root)) == name:
                    with open(path, "r", encoding="utf-8") as handle:
                        return handle.read()
        except Exception:
            pass
    try:
        hub = SkillHub.team_from_config(config)
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


def _storage_status(config) -> dict[str, Any]:
    backend = str(getattr(config, "sharing_backend", "") or "").strip().lower()
    endpoint = str(getattr(config, "sharing_viking_endpoint", "") or getattr(config, "sharing_endpoint", "") or "")
    deployment = str(getattr(config, "sharing_viking_deployment", "") or "cloud")
    namespace = "resources" if backend == "viking" else backend or "none"
    api_key_present = bool(
        str(getattr(config, "sharing_viking_team_api_key", "") or "")
        or str(getattr(config, "sharing_viking_api_key", "") or "")
    )
    payload: dict[str, Any] = {
        "backend": backend or "none",
        "deployment": deployment,
        "endpoint": endpoint,
        "namespace": namespace,
        "api_key_present": api_key_present,
        "reachable": False,
    }
    if not getattr(config, "sharing_enabled", False):
        payload["reason"] = "sharing_disabled"
        return payload
    try:
        hub = SkillHub.team_from_config(config)
        # Probe the configured store. Missing manifest is still a successful
        # connectivity check: it means the bucket/key is reachable but empty.
        try:
            hub._bucket.get_object(hub._manifest_key())
        except Exception as exc:  # noqa: BLE001
            if not is_not_found_error(exc):
                raise
        payload["reachable"] = True
        return payload
    except Exception as exc:  # noqa: BLE001
        payload["reason"] = str(exc)
        return payload


async def _ingest_session_dict(owner, session: dict[str, Any]) -> dict[str, Any]:
    """Shared ingest pipeline for one already-normalized session dict.

    Both the public ``/ingest_session`` endpoint and the Langfuse puller feed
    sessions through this single path so dedup, value classification, queueing,
    and the debounced evolve trigger behave identically regardless of source.

    The caller MUST have already set a sanitized ``session_id`` and any
    ``user_alias`` default. Returns the same status payload the endpoint emits.
    """
    session_id = str(session.get("session_id") or "")
    try:
        session_store = SessionStore.from_config(owner.config)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="session storage is not configured") from exc

    # Skip re-ingesting an already-processed session whose content has not
    # changed. A continued conversation (new turns) has a different fingerprint
    # and is ingested normally.
    force_reprocess = bool(session.pop("force_reprocess", False))
    if not force_reprocess and session_store.duplicate_of_processed(session):
        logger.info(
            "[SessionFilter] skipped duplicate session=%s (already processed, no new content)",
            session_id,
        )
        return {"status": "duplicate", "session_id": session_id, "queued": False}
    if force_reprocess:
        session["reprocess_reason"] = str(
            session.get("reprocess_reason") or "explicit dashboard reingest"
        )

    classifier = SessionValueClassifier.from_config(owner.config)
    value_judge = await classifier.classify(session)
    session["value_judge"] = value_judge
    session["ingested_at"] = _utc_now_iso()

    if value_judge.get("decision") != "valuable":
        session_store.save_skipped(session)
        _invalidate_dashboard_cache(f"conversations:{id(owner.config)}")
        logger.info(
            "[SessionFilter] skipped session=%s decision=%s reason=%s",
            session_id,
            value_judge.get("decision"),
            value_judge.get("reason"),
        )
        return {
            "status": "skipped",
            "session_id": session_id,
            "queued": False,
            "value_judge": value_judge,
        }

    key = session_store.save_queued(session)
    _invalidate_dashboard_cache(
        f"queue:{id(owner.config)}",
        f"conversations:{id(owner.config)}",
        f"status:{id(owner.config)}",
    )
    trigger_scheduled = (
        False
        if bool(session.get("defer_evolution_trigger"))
        else owner._schedule_evolve_trigger()
    )
    logger.info("[SessionFilter] queued valuable session=%s key=%s", session_id, key)
    return {
        "status": "queued",
        "session_id": session_id,
        "queued": True,
        "key": key,
        "trigger_scheduled": trigger_scheduled,
        "value_judge": value_judge,
    }


class RoutesMixin:
    """FastAPI app construction, routing, and request authentication."""

    def _build_app(self) -> FastAPI:
        owner = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            owner._ready_event.set()
            owner._start_skill_reload_polling()
            owner._start_embedded_evolve()
            owner._start_dreamcycle()
            try:
                owner._start_skillminer()
            except Exception:
                logger.debug("[SkillMiner] eager start failed", exc_info=True)
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
        dist_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "dist"))
        dist_index = os.path.join(dist_dir, "index.html")
        dist_assets = os.path.join(dist_dir, "assets")
        if os.path.isdir(dist_assets):
            app.mount("/assets", StaticFiles(directory=dist_assets), name="assets")
        docs_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "assets"))
        if os.path.isdir(docs_assets):
            app.mount("/docs-assets", StaticFiles(directory=docs_assets), name="docs-assets")

        def _session_user(request: Request) -> dict | None:
            token = request.cookies.get(_SESSION_COOKIE, "")
            if not token:
                return None
            session = owner._console_sessions.get(token)
            if not isinstance(session, dict):
                return None
            if float(session.get("expires_at", 0) or 0) < time.time():
                owner._console_sessions.pop(token, None)
                _save_console_sessions(owner.config, owner._console_sessions)
                return None
            user_id = str(session.get("user_id") or "")
            if not user_id:
                return None
            data = _load_registry(_registry_path(owner.config))
            try:
                _idx, user = _find_user(data, user_id)
            except HTTPException:
                owner._console_sessions.pop(token, None)
                _save_console_sessions(owner.config, owner._console_sessions)
                return None
            session["expires_at"] = time.time() + _SESSION_TTL_SECONDS
            return _public_user(user, owner.config)

        def _users_empty() -> bool:
            data = _load_registry(_registry_path(owner.config))
            return not bool(data.get("users"))

        @app.middleware("http")
        async def embedded_evolve_routes(request: Request, call_next):
            if _is_embedded_evolve_path(request.url.path):
                response = await owner._dispatch_embedded_evolve_request(request)
                if response is not None:
                    return response
            return await call_next(request)

        @app.middleware("http")
        async def require_console_auth(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and not path.startswith("/api/auth/"):
                if _users_empty():
                    return JSONResponse(status_code=401, content={"detail": "setup required", "needs_setup": True})
                user = _session_user(request)
                if user is None:
                    return JSONResponse(status_code=401, content={"detail": "login required"})
                request.state.console_user = user
            return await call_next(request)

        # Skill and user management REST APIs used by the unified console.
        self._register_skills_admin_routes(app)
        self._register_skill_lab_routes(app)
        self._register_users_admin_routes(app)
        self._register_openviking_workspace_routes(app)
        self._register_memory_debug_routes(app)
        self._register_agent_context_routes(app)
        self._register_skillminer_routes(app)
        self._register_docs_routes(app)

        @app.get("/")
        @app.get("/console")
        async def console():
            if os.path.isfile(dist_index):
                return FileResponse(dist_index)
            return JSONResponse(status_code=404, content={"detail": "teamEvolver console is not built"})

        @app.get("/v1/models")
        async def model_proxy_models(request: Request):
            _check_model_proxy_api_key(request)
            model = str(owner.config.llm_model_id or owner.config.model_name or "")
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
            _check_model_proxy_api_key(request)
            payload = _model_proxy_payload(owner.config, await request.json())
            url = _upstream_chat_url(owner.config)
            headers = _upstream_chat_headers(owner.config)
            timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
            if payload.get("stream"):
                client = httpx.AsyncClient(timeout=timeout)
                upstream = await client.send(
                    client.build_request("POST", url, headers=headers, json=payload),
                    stream=True,
                )
                if upstream.status_code >= 400:
                    error_body = await upstream.aread()
                    await upstream.aclose()
                    await client.aclose()
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

                return StreamingResponse(
                    stream_upstream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream = await client.post(url, headers=headers, json=payload)
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )

        @app.get("/api/auth/status")
        async def auth_status(request: Request):
            user = _session_user(request)
            return {
                "authenticated": bool(user),
                "needs_setup": _users_empty(),
                "user": user,
            }

        @app.post("/api/auth/bootstrap")
        async def auth_bootstrap(request: Request):
            if not _users_empty():
                raise HTTPException(status_code=409, detail="users already exist")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="bootstrap body must be an object")
            password = str(body.get("password") or "admin")
            payload = {
                "id": body.get("username") or body.get("id") or "admin",
                "display_name": body.get("display_name") or body.get("username") or "admin",
                "email": body.get("email") or "",
                "role": "admin",
                "password": password,
            }
            path = _registry_path(owner.config)
            data = _load_registry(path)
            user = _upsert_user(data, payload, config=owner.config)
            _save_registry(path, data)
            sync_openviking_user(owner.config, str(user.get("id") or ""))
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            _save_console_sessions(owner.config, owner._console_sessions)
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            resp.set_cookie(
                _SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                max_age=_SESSION_TTL_SECONDS,
                path="/",
            )
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
            data = _load_registry(_registry_path(owner.config))
            try:
                _idx, user = _find_user(data, username)
            except HTTPException as exc:
                raise HTTPException(status_code=401, detail="invalid username or password") from exc
            if not user.get("password_hash") or not _verify_password(password, str(user.get("password_hash") or "")):
                raise HTTPException(status_code=401, detail="invalid username or password")
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            _save_console_sessions(owner.config, owner._console_sessions)
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            resp.set_cookie(
                _SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                max_age=_SESSION_TTL_SECONDS,
                path="/",
            )
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
            data = _load_registry(path)
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
            _save_registry(path, data)
            sync_openviking_user(owner.config, str(user.get("id") or ""))
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            _save_console_sessions(owner.config, owner._console_sessions)
            resp = JSONResponse(
                content={
                    "authenticated": True,
                    "needs_setup": False,
                    "user": _public_user(user, owner.config),
                }
            )
            resp.set_cookie(
                _SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                max_age=_SESSION_TTL_SECONDS,
                path="/",
            )
            return resp

        @app.post("/api/auth/logout")
        async def auth_logout(request: Request):
            token = request.cookies.get(_SESSION_COOKIE, "")
            if token:
                owner._console_sessions.pop(token, None)
                _save_console_sessions(owner.config, owner._console_sessions)
            resp = JSONResponse(content={"authenticated": False})
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
            _require_admin_user(_session_user(request))
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

        @app.get("/api/evolve-model")
        async def api_get_evolve_model():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            return JSONResponse(content=_model_settings_payload(owner.config, store.load()))

        @app.post("/api/evolve-model")
        async def api_save_evolve_model(request: Request):
            _require_admin_user(_session_user(request))
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
            try:
                max_tokens = max(1, int(body.get("max_tokens") or owner.config.llm_max_tokens or 100000))
                temperature = float(
                    body.get("temperature")
                    if body.get("temperature") is not None
                    else owner.config.llm_temperature
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid max_tokens or temperature") from exc
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
            existing_key = str(llm.get("api_key") or owner.config.llm_api_key or "")
            raw_key = body.get("api_key")
            clear_key = bool(body.get("clear_api_key", False))
            api_key = "" if clear_key else existing_key
            if raw_key is not None and str(raw_key).strip():
                api_key = str(raw_key).strip()
            llm.update(
                {
                    "provider": provider,
                    "api_base": base_url,
                    "model_id": model,
                    "api_key": api_key,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
            )
            store.save(data)
            await owner._reload_openviking_integrations(store.to_config())
            owner._stop_skillminer()
            return JSONResponse(content=_model_settings_payload(owner.config, data))

        @app.get("/api/evolve-settings")
        async def api_get_evolve_settings():
            config_file = str(
                getattr(owner.config, "_config_file", "") or ""
            ).strip()
            store = (
                ConfigStore(config_file=Path(config_file))
                if config_file
                else ConfigStore()
            )
            return JSONResponse(
                content=_evolve_settings_payload(owner.config, store.load())
            )

        @app.post("/api/evolve-settings")
        async def api_save_evolve_settings(request: Request):
            _require_admin_user(_session_user(request))
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
            if "model" in memory_in:
                dreamcycle["llm_model"] = str(memory_in.get("model") or "").strip()
            if "base_url" in memory_in:
                dreamcycle["llm_base_url"] = str(
                    memory_in.get("base_url") or ""
                ).strip()
            if "llm_max_tokens" in memory_in:
                dreamcycle["llm_max_tokens"] = _bounded_int(
                    memory_in,
                    "llm_max_tokens",
                    dreamcycle.get("llm_max_tokens", 4096),
                    minimum=1,
                    maximum=131072,
                )
            if "temperature" in memory_in:
                try:
                    temperature = float(memory_in["temperature"])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail="DreamCycle temperature must be numeric",
                    ) from exc
                dreamcycle["temperature"] = max(
                    0.0,
                    min(2.0, temperature),
                )
            if "customer_id" in memory_in:
                dreamcycle["customer_id"] = str(
                    memory_in.get("customer_id") or ""
                ).strip()
                dreamcycle.pop("peer_id", None)
            for source_key, target_key in (
                ("embed_model", "embed_model"),
                ("embed_base_url", "embed_base_url"),
            ):
                if source_key in memory_in:
                    dreamcycle[target_key] = str(
                        memory_in.get(source_key) or ""
                    ).strip()
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

            scheduler_in = memory_in.get("scheduler")
            if scheduler_in is not None and not isinstance(
                scheduler_in,
                dict,
            ):
                raise HTTPException(
                    status_code=400,
                    detail="memory_maintenance.scheduler must be an object",
                )
            scheduler_in = scheduler_in or {}
            for key, minimum, maximum, fallback in (
                ("active_start_hour", 0, 23, 0),
                ("active_end_hour", 0, 23, 6),
                ("rounds_per_window", 1, 24, 3),
                ("round_interval_minutes", 1, 1440, 90),
                ("max_turns_per_job", 1, 200, 25),
                ("max_consecutive_errors", 1, 100, 3),
                ("retry_delay_seconds", 1, 86400, 300),
            ):
                dreamcycle[key] = _bounded_int(
                    scheduler_in,
                    key,
                    dreamcycle.get(key, fallback),
                    minimum=minimum,
                    maximum=maximum,
                )

            jobs_in = memory_in.get("jobs")
            if jobs_in is not None:
                if not isinstance(jobs_in, list):
                    raise HTTPException(
                        status_code=400,
                        detail="memory_maintenance.jobs must be a list",
                    )
                from ..integrations.dreamcycle_runtime import available_jobs

                catalog = {
                    job["id"]: job
                    for job in available_jobs(
                        str(
                            getattr(
                                owner.config,
                                "team_display_name",
                                "",
                            )
                            or "Team"
                        )
                    )
                }
                enabled_jobs: list[str] = []
                job_prompts: dict[str, str] = {}
                job_settings: dict[str, dict[str, Any]] = {}
                default_runtime = {
                    "model": "",
                    "base_url": "",
                    "temperature": float(
                        dreamcycle.get("temperature", 0.3)
                        if dreamcycle.get("temperature") is not None
                        else 0.3
                    ),
                    "max_tokens": int(
                        dreamcycle.get("llm_max_tokens", 4096) or 4096
                    ),
                    "max_turns": int(
                        dreamcycle.get("max_turns_per_job", 25) or 25
                    ),
                    "max_errors": int(
                        dreamcycle.get("max_consecutive_errors", 3) or 3
                    ),
                }
                for raw_job in jobs_in:
                    if not isinstance(raw_job, dict):
                        continue
                    job_id = str(raw_job.get("id") or "").strip()
                    if job_id not in catalog:
                        raise HTTPException(
                            status_code=400,
                            detail=f"unknown DreamCycle job: {job_id}",
                        )
                    if bool(raw_job.get("enabled", True)):
                        enabled_jobs.append(job_id)
                    prompt = str(
                        raw_job.get("effective_prompt")
                        or raw_job.get("prompt")
                        or ""
                    ).strip()
                    if not prompt:
                        raise HTTPException(
                            status_code=400,
                            detail=f"DreamCycle {job_id} prompt cannot be empty",
                        )
                    if prompt != str(catalog[job_id]["default_prompt"]):
                        job_prompts[job_id] = prompt
                    runtime = raw_job.get("runtime")
                    if runtime is not None and not isinstance(runtime, dict):
                        raise HTTPException(
                            status_code=400,
                            detail=f"DreamCycle {job_id} runtime must be an object",
                        )
                    runtime = runtime or {}
                    try:
                        float(
                            runtime.get(
                                "temperature",
                                default_runtime["temperature"],
                            )
                        )
                        for numeric_key in (
                            "max_tokens",
                            "max_turns",
                            "max_errors",
                        ):
                            int(
                                runtime.get(
                                    numeric_key,
                                    default_runtime[numeric_key],
                                )
                            )
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"DreamCycle {job_id} runtime numeric "
                                "parameters are invalid"
                            ),
                        ) from exc
                    normalized_runtime = {
                        "model": str(runtime.get("model") or "").strip(),
                        "base_url": str(
                            runtime.get("base_url") or ""
                        ).strip(),
                        "temperature": max(
                            0.0,
                            min(
                                2.0,
                                float(
                                    runtime.get(
                                        "temperature",
                                        default_runtime["temperature"],
                                    )
                                ),
                            ),
                        ),
                        "max_tokens": max(
                            1,
                            min(
                                131072,
                                int(
                                    runtime.get(
                                        "max_tokens",
                                        default_runtime["max_tokens"],
                                    )
                                ),
                            ),
                        ),
                        "max_turns": max(
                            1,
                            min(
                                200,
                                int(
                                    runtime.get(
                                        "max_turns",
                                        default_runtime["max_turns"],
                                    )
                                ),
                            ),
                        ),
                        "max_errors": max(
                            1,
                            min(
                                100,
                                int(
                                    runtime.get(
                                        "max_errors",
                                        default_runtime["max_errors"],
                                    )
                                ),
                            ),
                        ),
                    }
                    if normalized_runtime != default_runtime:
                        job_settings[job_id] = normalized_runtime
                dreamcycle["enabled_jobs"] = enabled_jobs
                dreamcycle["job_prompts"] = job_prompts
                dreamcycle["job_settings"] = job_settings

            # Deprecated simplified-engine settings remain accepted so old
            # clients can upgrade without a coordinated deployment.
            for key, minimum, maximum, fallback in (
                ("interval_seconds", 60, 365 * 86400, 86400),
                ("max_source_items", 1, 10000, 100),
                ("max_source_chars", 1000, 10_000_000, 120000),
            ):
                dreamcycle[key] = _bounded_int(
                    memory_in,
                    key,
                    dreamcycle.get(key, fallback),
                    minimum=minimum,
                    maximum=maximum,
                )
            if "prompts" in memory_in:
                prompt_input = memory_in.get("prompts")
                if not isinstance(prompt_input, dict):
                    raise HTTPException(
                        status_code=400,
                        detail="memory_maintenance.prompts must be an object",
                    )
                prompts = dreamcycle.setdefault("prompts", {})
                for key in ("extract", "consolidate"):
                    if key in prompt_input:
                        value = str(prompt_input.get(key) or "").strip()
                        if not value:
                            raise HTTPException(
                                status_code=400,
                                detail=f"DreamCycle {key} prompt cannot be empty",
                            )
                        prompts[key] = value
            existing_key = str(
                dreamcycle.get("llm_api_key")
                or getattr(owner.config, "dreamcycle_llm_api_key", "")
                or ""
            )
            if bool(memory_in.get("clear_api_key", False)):
                existing_key = ""
            if str(memory_in.get("api_key") or "").strip():
                existing_key = str(memory_in["api_key"]).strip()
            dreamcycle["llm_api_key"] = existing_key
            existing_embed_key = str(
                dreamcycle.get("embed_api_key")
                or getattr(owner.config, "dreamcycle_embed_api_key", "")
                or ""
            )
            if bool(memory_in.get("clear_embed_api_key", False)):
                existing_embed_key = ""
            if str(memory_in.get("embed_api_key") or "").strip():
                existing_embed_key = str(
                    memory_in["embed_api_key"]
                ).strip()
            dreamcycle["embed_api_key"] = existing_embed_key

            store.save(data)
            await owner._reload_openviking_integrations(store.to_config())
            return JSONResponse(
                content=_evolve_settings_payload(owner.config, data)
            )

        @app.get("/api/langfuse-config")
        async def api_get_langfuse_config():
            config_file = str(getattr(owner.config, "_config_file", "") or "").strip()
            store = ConfigStore(config_file=Path(config_file)) if config_file else ConfigStore()
            return JSONResponse(content=_langfuse_settings_payload(owner.config, store.load()))

        @app.post("/api/langfuse-config")
        async def api_save_langfuse_config(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="langfuse settings body must be an object")

            enabled = bool(body.get("enabled", False))
            tracing_enabled = bool(body.get("tracing_enabled", False))
            host = str(body.get("host") or "").strip().rstrip("/") or "https://cloud.langfuse.com"

            def _norm_list(value: Any) -> list[str]:
                if isinstance(value, (list, tuple, set)):
                    items = value
                elif value in (None, ""):
                    items = []
                else:
                    items = str(value).replace("\n", ",").split(",")
                seen: list[str] = []
                for raw in items:
                    item = str(raw or "").strip()
                    if item and item not in seen:
                        seen.append(item)
                return seen

            try:
                max_sessions = max(1, int(body.get("max_sessions") or owner.config.langfuse_max_sessions or 100))
                page_limit = max(1, min(100, int(body.get("page_limit") or owner.config.langfuse_page_limit or 50)))
                timeout_seconds = max(
                    1, int(body.get("timeout_seconds") or owner.config.langfuse_timeout_seconds or 30)
                )
                tracing_sample_rate = max(
                    0.0,
                    min(
                        1.0,
                        float(body.get("tracing_sample_rate", 1.0)),
                    ),
                )
                tracing_flush_at = max(
                    1, int(body.get("tracing_flush_at") or 1)
                )
                tracing_flush_interval_seconds = max(
                    0.1,
                    float(
                        body.get("tracing_flush_interval_seconds")
                        or 1.0
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "invalid Langfuse paging, timeout, sampling, or flush "
                        "settings"
                    ),
                ) from exc

            # Enabling requires usable credentials so the UI never claims "enabled"
            # while /langfuse/* would immediately 401.
            store = ConfigStore()
            data = store.load()
            langfuse = data.setdefault("langfuse", {})

            existing_public = str(langfuse.get("public_key") or owner.config.langfuse_public_key or "")
            existing_secret = str(langfuse.get("secret_key") or owner.config.langfuse_secret_key or "")
            clear_public = bool(body.get("clear_public_key", False))
            clear_secret = bool(body.get("clear_secret_key", False))
            public_key = "" if clear_public else existing_public
            secret_key = "" if clear_secret else existing_secret
            raw_public = body.get("public_key")
            raw_secret = body.get("secret_key")
            if raw_public is not None and str(raw_public).strip():
                public_key = str(raw_public).strip()
            if raw_secret is not None and str(raw_secret).strip():
                secret_key = str(raw_secret).strip()

            if (enabled or tracing_enabled) and (not public_key or not secret_key):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "public_key 和 secret_key 均为必填项才能启用 "
                        "Langfuse 会话拉取或链路观测"
                    ),
                )

            # Operator-authored trace mapper. Persist the code even while
            # disabled (so a draft survives), but reject enabling code that does
            # not compile / lacks a map_trace entry point, mirroring how the
            # console dry-run tester reports errors.
            mapper_enabled = bool(body.get("mapper_enabled", langfuse.get("mapper_enabled", False)))
            if "mapper_code" in body:
                mapper_code = str(body.get("mapper_code") or "")
            else:
                mapper_code = str(langfuse.get("mapper_code") or "")
            if mapper_enabled and mapper_code.strip():
                from ..integrations.langfuse_mapper import MapperError, compile_mapper

                try:
                    compile_mapper(mapper_code)
                except MapperError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"trace mapper 无法启用：{exc}",
                    ) from exc
            elif mapper_enabled and not mapper_code.strip():
                raise HTTPException(
                    status_code=400,
                    detail="启用自定义 trace mapper 前必须填写 map_trace 代码",
                )

            langfuse.update(
                {
                    "enabled": enabled,
                    "host": host,
                    "public_key": public_key,
                    "secret_key": secret_key,
                    "tracing_enabled": tracing_enabled,
                    "tracing_environment": str(
                        body.get("tracing_environment") or "local"
                    ).strip(),
                    "tracing_release": str(
                        body.get("tracing_release") or ""
                    ).strip(),
                    "tracing_sample_rate": tracing_sample_rate,
                    "tracing_capture_content": bool(
                        body.get("tracing_capture_content", True)
                    ),
                    "tracing_flush_at": tracing_flush_at,
                    "tracing_flush_interval_seconds": (
                        tracing_flush_interval_seconds
                    ),
                    "max_sessions": max_sessions,
                    "page_limit": page_limit,
                    "timeout_seconds": timeout_seconds,
                    "default_environment": _norm_list(body.get("default_environment")),
                    "default_user_id": str(body.get("default_user_id") or "").strip(),
                    "default_tags": _norm_list(body.get("default_tags")),
                    "default_release": str(body.get("default_release") or "").strip(),
                    "default_version": str(body.get("default_version") or "").strip(),
                    "default_trace_name": str(body.get("default_trace_name") or "").strip(),
                    "mapper_enabled": mapper_enabled,
                    "mapper_code": mapper_code,
                }
            )
            store.save(data)
            # Hot-reload the in-memory config so /langfuse/* endpoints pick up the
            # new host/keys/filters immediately, without a service restart.
            owner._configure_langfuse(store.to_config())
            return JSONResponse(content=_langfuse_settings_payload(owner.config, data))

        @app.post("/api/langfuse-config/test")
        async def api_test_langfuse_config(request: Request):
            _require_admin_user(_session_user(request))
            from ..integrations.langfuse_client import LangfuseClient, LangfuseError

            body = await request.json() if await request.body() else {}
            if not isinstance(body, dict):
                body = {}
            host = str(body.get("host") or owner.config.langfuse_host or "").strip().rstrip("/")
            public_key = str(body.get("public_key") or "").strip() or str(owner.config.langfuse_public_key or "")
            secret_key = str(body.get("secret_key") or "").strip() or str(owner.config.langfuse_secret_key or "")
            if not host or not public_key or not secret_key:
                raise HTTPException(
                    status_code=400, detail="host / public_key / secret_key 均为测试连通性的必填项"
                )
            try:
                health = await asyncio.to_thread(
                    lambda: LangfuseClient(
                        host=host,
                        public_key=public_key,
                        secret_key=secret_key,
                        timeout=float(owner.config.langfuse_timeout_seconds or 30),
                    ).health()
                )
            except LangfuseError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"langfuse test failed: {exc}") from exc
            return JSONResponse(
                content={"ok": True, "host": host, "total_sessions": health.get("total_sessions")}
            )

        # ---- Prompt Studio: transparent, editable, testable pipeline ---- #
        def _prompt_studio():
            from ..evolve import prompt_studio as ps

            return ps

        def _load_studio_session(session_id: str) -> dict[str, Any]:
            """Load a full session dict (turns) to use as test input."""
            store = SessionStore.from_config(owner.config)
            session = store.load_session(_safe_session_id(session_id))
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
            return session

        def _studio_llm_factory():
            from ..llm import AsyncLLMClient

            api_key = str(getattr(owner.config, "llm_api_key", "") or "")
            base_url = str(getattr(owner.config, "llm_api_base", "") or "")
            model = str(getattr(owner.config, "llm_model_id", "") or getattr(owner.config, "model_name", "") or "")
            if not api_key or not base_url or not model:
                raise HTTPException(
                    status_code=503,
                    detail="进化模型未配置，无法测试 prompt。请先在「全局模型」中配置。",
                )
            return AsyncLLMClient(
                api_key=api_key,
                base_url=base_url,
                model=model,
                max_tokens=int(getattr(owner.config, "llm_max_tokens", 100000) or 100000),
                temperature=float(getattr(owner.config, "llm_temperature", 0.4) or 0.4),
            )

        @app.get("/api/prompt-studio/pipeline")
        async def api_prompt_studio_pipeline():
            return JSONResponse(content=_prompt_studio().pipeline_graph())

        @app.get("/api/prompt-studio/prompts")
        async def api_prompt_studio_prompts():
            return JSONResponse(content={"prompts": _prompt_studio().list_prompts()})

        @app.get("/api/prompt-studio/prompts/{stage_id}")
        async def api_prompt_studio_prompt_detail(stage_id: str):
            try:
                return JSONResponse(content=_prompt_studio().get_prompt(stage_id))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc

        @app.post("/api/prompt-studio/prompts/{stage_id}")
        async def api_prompt_studio_save(stage_id: str, request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be an object")
            ps = _prompt_studio()
            try:
                if "prompt" in body:
                    ps.set_override(stage_id, str(body.get("prompt") or ""))
                if "settings" in body:
                    ps.set_stage_settings(stage_id, body.get("settings"))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(content=ps.get_prompt(stage_id))

        @app.post("/api/prompt-studio/prompts/{stage_id}/reset")
        async def api_prompt_studio_reset(stage_id: str, request: Request):
            _require_admin_user(_session_user(request))
            ps = _prompt_studio()
            try:
                ps.reset_override(stage_id)
                ps.reset_stage_settings(stage_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            return JSONResponse(content=ps.get_prompt(stage_id))

        @app.get("/api/prompt-studio/sessions")
        async def api_prompt_studio_sessions(limit: int = 20):
            """Recent sessions the operator can use as test input."""
            try:
                store = SessionStore.from_config(owner.config)
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
                    "timestamp": r.get("ingested_at") or r.get("timestamp"),
                }
                for r in rows
            ]
            return JSONResponse(content={"sessions": sessions})

        @app.post("/api/prompt-studio/prompts/{stage_id}/test")
        async def api_prompt_studio_test(stage_id: str, request: Request):
            _require_admin_user(_session_user(request))
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
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"unknown prompt stage: {stage_id}") from exc
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"prompt test failed: {exc}") from exc
            return JSONResponse(content=result)

        @app.post("/api/evolve-model/test")
        async def api_test_evolve_model(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            store = ConfigStore()
            data = store.load()
            llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
            base_url = str(body.get("base_url") or owner.config.llm_api_base or llm.get("api_base") or "").strip()
            model = str(body.get("model") or owner.config.llm_model_id or llm.get("model_id") or "").strip()
            raw_key = body.get("api_key")
            api_key = (
                str(raw_key).strip()
                if raw_key is not None and str(raw_key).strip()
                else str(owner.config.llm_api_key or llm.get("api_key") or "")
            )
            if not base_url or not model or not api_key:
                raise HTTPException(status_code=400, detail="base_url, model and api_key are required for test")
            try:
                from openai import OpenAI

                client = OpenAI(api_key=api_key, base_url=base_url)
                started = time.time()
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
                    resp = client.chat.completions.create(**payload)
                except Exception as first_exc:
                    body_text = getattr(getattr(first_exc, "response", None), "text", "") or ""
                    if "'temperature' is not supported" in body_text:
                        payload.pop("temperature", None)
                        resp = client.chat.completions.create(**payload)
                    elif "max_completion_tokens" in body_text:
                        payload["max_tokens"] = payload.pop("max_completion_tokens")
                        resp = client.chat.completions.create(**payload)
                    else:
                        raise
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

        @app.post("/ingest_session")
        async def ingest_session(request: Request):
            body = await _read_limited_json_body(request)
            agent_record: dict[str, Any] | None = None
            if is_v1_payload(body):
                agent_record = verify_agent_access_token(
                    owner.config,
                    _bearer_token(request),
                    required_scope="session.ingest",
                )
                if agent_record is None:
                    raise HTTPException(
                        status_code=401,
                        detail="invalid or insufficient Agent access token",
                    )
            else:
                _check_ingest_api_key(request)
            try:
                body = normalize_session_envelope(body)
            except AgentProtocolError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session_id = _safe_session_id(body.get("session_id"))
            session = dict(body)
            session["session_id"] = session_id
            session.setdefault("user_alias", str(getattr(owner.config, "sharing_user_alias", "") or "anonymous"))
            runtime = session.get("runtime") if isinstance(session.get("runtime"), dict) else {}
            if agent_record is not None and str(
                runtime.get("integration_id") or ""
            ) != str(agent_record.get("agent_id") or ""):
                raise HTTPException(
                    status_code=403,
                    detail="session runtime.integration_id does not match access token",
                )
            runtime_context = (
                dict(session.get("runtime_context"))
                if isinstance(session.get("runtime_context"), dict)
                else {}
            )
            runtime_type = str(runtime.get("type") or session.get("source") or "")
            external_username = str(
                runtime_context.get("username")
                or session.get("user_alias")
                or ""
            )
            if agent_record is not None:
                local_user_id = resolve_agent_subject_user_id(
                    owner.config,
                    integration_id=str(agent_record.get("agent_id") or ""),
                    runtime_type=runtime_type,
                    external_subject=str(
                        runtime_context.get("external_subject")
                        or external_username
                    ),
                    allow_legacy_runtime_mapping=False,
                )
                if not local_user_id:
                    raise HTTPException(
                        status_code=403,
                        detail="SUBJECT_NOT_MAPPED",
                    )
            else:
                local_user_id = resolve_registered_user_id(
                    owner.config,
                    runtime_type=runtime_type,
                    external_username=external_username,
                    preferred_user_id=str(
                        runtime_context.get("team_evolver_user_id") or ""
                    ),
                )
            if local_user_id:
                runtime_context["team_evolver_user_id"] = local_user_id
                session["runtime_context"] = runtime_context
            if agent_record is not None:
                try:
                    session["turns"] = verify_context_usage(
                        owner.config,
                        agent_id=str(agent_record.get("agent_id") or ""),
                        user_id=local_user_id,
                        turns=[
                            dict(turn)
                            for turn in session.get("turns") or []
                            if isinstance(turn, dict)
                        ],
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"invalid context_usage: {exc}",
                    ) from exc
            return await _ingest_session_dict(owner, session)

        async def _ingest_langfuse_session(session: dict[str, Any]) -> dict[str, Any]:
            """In-process ingest for one converted Langfuse session dict."""
            session = dict(session)
            session["session_id"] = _safe_session_id(session.get("session_id"))
            session.setdefault(
                "user_alias",
                str(getattr(owner.config, "sharing_user_alias", "") or "langfuse"),
            )
            return await _ingest_session_dict(owner, session)

        def _langfuse_filter_overrides(source: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(source, dict):
                return {}
            overrides: dict[str, Any] = {}
            for key in (
                "from_timestamp",
                "to_timestamp",
                "environment",
                "user_id",
                "tags",
                "release",
                "version",
                "trace_name",
                "session_id",
                "metadata",
            ):
                if source.get(key) not in (None, ""):
                    overrides[key] = source.get(key)
            return overrides

        @app.get("/langfuse/status")
        async def langfuse_status():
            from ..integrations.langfuse_client import LangfuseClient, LangfuseError
            from ..observability import langfuse_status as tracing_status

            enabled = bool(getattr(owner.config, "langfuse_enabled", False))
            payload: dict[str, Any] = {
                "enabled": enabled,
                "tracing": tracing_status(),
                "host": str(getattr(owner.config, "langfuse_host", "") or ""),
                "public_key_present": bool(getattr(owner.config, "langfuse_public_key", "")),
                "secret_key_present": bool(getattr(owner.config, "langfuse_secret_key", "")),
                "max_sessions": int(getattr(owner.config, "langfuse_max_sessions", 100) or 100),
                "default_filters": {
                    "environment": list(getattr(owner.config, "langfuse_default_environment", []) or []),
                    "user_id": str(getattr(owner.config, "langfuse_default_user_id", "") or ""),
                    "tags": list(getattr(owner.config, "langfuse_default_tags", []) or []),
                    "release": str(getattr(owner.config, "langfuse_default_release", "") or ""),
                    "version": str(getattr(owner.config, "langfuse_default_version", "") or ""),
                    "trace_name": str(getattr(owner.config, "langfuse_default_trace_name", "") or ""),
                },
                "reachable": False,
            }
            if not enabled:
                payload["reason"] = "langfuse_disabled"
                return JSONResponse(content=payload)
            try:
                health = await asyncio.to_thread(
                    lambda: LangfuseClient.from_config(owner.config).health()
                )
                payload["reachable"] = True
                payload["total_sessions"] = health.get("total_sessions")
            except LangfuseError as exc:
                payload["reason"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                payload["reason"] = str(exc)
            return JSONResponse(content=payload)

        @app.post("/langfuse/sessions")
        async def langfuse_sessions(request: Request):
            from ..integrations.langfuse_pull import preview_sessions

            body = await request.json() if await request.body() else {}
            if not isinstance(body, dict):
                body = {}
            overrides = _langfuse_filter_overrides(body)
            try:
                max_sessions = int(body.get("max_sessions") or 0)
            except (TypeError, ValueError):
                max_sessions = 0
            try:
                result = await asyncio.to_thread(
                    preview_sessions,
                    owner.config,
                    overrides,
                    max_sessions=max_sessions,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"langfuse list failed: {exc}") from exc
            return JSONResponse(content=result)

        @app.post("/langfuse/pull")
        async def langfuse_pull(request: Request):
            _check_ingest_api_key(request)
            from ..integrations.langfuse_pull import pull_sessions

            body = await request.json() if await request.body() else {}
            if not isinstance(body, dict):
                body = {}
            overrides = _langfuse_filter_overrides(body)
            try:
                max_sessions = int(body.get("max_sessions") or 0)
            except (TypeError, ValueError):
                max_sessions = 0
            try:
                result = await pull_sessions(
                    owner.config,
                    _ingest_langfuse_session,
                    overrides,
                    max_sessions=max_sessions,
                    user_alias=str(body.get("user_alias") or ""),
                    force_reprocess=bool(body.get("force_reprocess", False)),
                    defer_evolution_trigger=bool(body.get("defer_evolution_trigger", False)),
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"langfuse pull failed: {exc}") from exc
            return JSONResponse(content=result)

        @app.get("/langfuse/mapper/template")
        async def langfuse_mapper_template():
            """Return the reference mapper code, bundled sample, and format spec."""
            from ..integrations.langfuse_mapper import (
                TURN_KEYS,
                default_mapper_code,
                sample_trace_payload,
                standard_format_spec,
            )

            return JSONResponse(
                content={
                    "template": default_mapper_code(),
                    "sample": sample_trace_payload(),
                    "turn_keys": list(TURN_KEYS),
                    "spec": standard_format_spec(),
                }
            )

        @app.post("/langfuse/mapper/test")
        async def langfuse_mapper_test(request: Request):
            """Dry-run an operator's trace mapper against a pasted or sample trace.

            Admin-only (the mapper is executable config). The request body is
            ``{"code": "...", "trace": <{trace, observations} | trace dict>?}``;
            when ``trace`` is omitted a small bundled sample is used so the
            operator can iterate before wiring up a live Langfuse pull.
            """
            _require_admin_user(_session_user(request))
            from ..integrations.langfuse_mapper import (
                run_mapper_preview,
                sample_trace_payload,
            )

            body = await request.json() if await request.body() else {}
            if not isinstance(body, dict):
                body = {}
            code = str(body.get("code") or "")
            if not code.strip():
                raise HTTPException(status_code=400, detail="mapper code is required")
            payload = body.get("trace")
            used_sample = payload in (None, "", {}, [])
            if used_sample:
                payload = sample_trace_payload()
            try:
                turn_num = max(1, int(body.get("turn_num") or 1))
            except (TypeError, ValueError):
                turn_num = 1
            result = await asyncio.to_thread(
                run_mapper_preview, code, payload, turn_num=turn_num
            )
            result["used_sample"] = used_sample
            return JSONResponse(content=result)

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
            access_token = ""
            if str(agent.get("compatibility") or "") == "compatible" and set(
                agent.get("capability_ids") or []
            ).intersection({"session.ingest.v1", "context.workspace.v1"}):
                agent, access_token = issue_agent_access_token(
                    owner.config,
                    agent_id=str(agent.get("agent_id") or ""),
                    rotate=bool(body.get("rotate_access_token", False)),
                )
            registration_fields: dict[str, Any] = {
                "agent": public_agent_record(agent),
            }
            if access_token:
                registration_fields["credentials"] = {
                    "agent_access_token": access_token,
                    "token_type": "Bearer",
                    "scopes": list(
                        (agent.get("access_auth") or {}).get("scopes") or []
                    ),
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
                    "personal_source_count": len(
                        durable_config.sharing_viking_personal_api_keys or []
                    ),
                }

            endpoint = str(storage.get("endpoint") or "").strip()
            account = str(storage.get("account") or "").strip()
            personal_user = str(storage.get("personal_user") or "").strip()
            configured_team_user = str(storage.get("team_user") or "").strip()
            personal_key = str(storage.get("personal_api_key") or "").strip()
            raw_personal_keys = storage.get("personal_api_keys")
            personal_keys_in = (
                [str(item or "").strip() for item in raw_personal_keys]
                if isinstance(raw_personal_keys, list)
                else []
            )
            team_key = str(storage.get("team_api_key") or "").strip()
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
                    detail="storage.endpoint and storage.team_api_key must be provided together",
                )

            from ..integrations.dreamcycle import parse_openviking_key

            key_account, encoded_team_user = parse_openviking_key(team_key)
            team_user = encoded_team_user or configured_team_user
            if not team_user:
                raise HTTPException(
                    status_code=400,
                    detail="storage.team_user is required when the team key has no encoded user",
                )
            existing = sharing.get("viking_personal_api_keys")
            source_keys = (
                list(existing)
                if isinstance(existing, list)
                else (
                    str(existing).replace("\n", ",").split(",")
                    if existing
                    else []
                )
            )
            legacy_personal = str(
                sharing.get("viking_personal_api_key") or ""
            ).strip()
            source_keys.extend([legacy_personal, personal_key, *personal_keys_in])
            source_keys = list(
                dict.fromkeys(
                    key
                    for raw in source_keys
                    if (key := str(raw or "").strip()) and key != team_key
                )
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
                    "viking_personal_api_keys": source_keys,
                }
            )
            if personal_key:
                sharing["viking_personal_api_key"] = personal_key
            data.setdefault("dreamcycle", {}).update(
                {"enabled": True, "auto_start": True}
            )
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
                "personal_source_count": len(source_keys),
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
                        "personal_api_key": body.get("personal_api_key"),
                        "personal_api_keys": body.get("personal_api_keys"),
                        "team_api_key": body.get("team_api_key"),
                    },
                }
            )

        @app.get("/api/agent-integrations")
        async def api_agent_integrations():
            skill_outbox = (
                SkillMutationService.from_config(owner.config).health()
                if getattr(owner.config, "sharing_enabled", False)
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
                "skill_sync_outbox": skill_outbox,
                "storage_authority": "teamEvolver",
                "storage_deployment": str(
                    getattr(owner.config, "sharing_viking_deployment", "") or "cloud"
                ),
                "default_team_user": str(
                    getattr(owner.config, "sharing_viking_user", "") or "team"
                ),
            }

        @app.post("/api/agent-integrations/skill-sync/{event_id}/retry")
        async def api_retry_skill_sync(
            event_id: str,
            body: dict[str, Any],
            request: Request,
        ):
            _require_admin_user(getattr(request.state, "console_user", None))
            try:
                event = SkillMutationService.from_config(owner.config).retry(
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
                event = SkillMutationService.from_config(owner.config).discard(
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

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.post("/trigger-dreamcycle")
        async def trigger_dreamcycle():
            result = owner._trigger_dreamcycle()
            status = str(result.get("status") or "")
            if status == "not_configured":
                return JSONResponse(content=result, status_code=503)
            return JSONResponse(content=result, status_code=202)

        @app.get("/trigger-dreamcycle/status")
        async def dreamcycle_status():
            return owner._dreamcycle_status()

        @app.get("/trigger-dreamcycle/dry-run")
        async def dreamcycle_dry_run(request: Request):
            _require_admin_user(_session_user(request))
            return owner._dreamcycle_dry_run()

        @app.get("/trigger-dreamcycle/memory-changes")
        async def dreamcycle_memory_changes(
            request: Request,
            limit: int = 100,
        ):
            _require_admin_user(_session_user(request))
            if not 1 <= limit <= 500:
                raise HTTPException(
                    status_code=400,
                    detail="limit must be between 1 and 500",
                )
            return owner._dreamcycle_memory_changes(limit=limit)

        @app.post(
            "/trigger-dreamcycle/memory-changes/{change_id}/true-replay"
        )
        async def dreamcycle_memory_true_replay(
            change_id: str,
            request: Request,
        ):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Memory True Replay body must be an object",
                )
            raw_checklist = body.get("checklist")
            if isinstance(raw_checklist, str):
                checklist = [
                    line.strip()
                    for line in raw_checklist.splitlines()
                    if line.strip()
                ]
            elif isinstance(raw_checklist, list):
                checklist = list(raw_checklist)
            else:
                checklist = []
            try:
                return await asyncio.to_thread(
                    owner._run_dreamcycle_memory_replay,
                    change_id=change_id,
                    query=str(body.get("query") or ""),
                    checklist=checklist,
                    source_session_id=str(
                        body.get("source_session_id") or ""
                    ),
                    max_interactions=int(
                        body.get("max_interactions") or 4
                    ),
                    timeout_seconds=int(
                        body.get("timeout_seconds") or 600
                    ),
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=str(exc),
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=str(exc),
                ) from exc

        @app.get(
            "/trigger-dreamcycle/memory-changes/{change_id}/true-replays"
        )
        async def dreamcycle_memory_true_replays(
            change_id: str,
            request: Request,
            limit: int = 100,
        ):
            _require_admin_user(_session_user(request))
            if not 1 <= limit <= 500:
                raise HTTPException(
                    status_code=400,
                    detail="limit must be between 1 and 500",
                )
            return owner._dreamcycle_memory_replays(
                change_id=change_id,
                limit=limit,
            )

        @app.post("/api/openviking/memory/true-replay")
        async def memory_adhoc_true_replay(request: Request):
            """A/B replay an unsaved Memory edit: stored version vs draft.

            Backs the workspace "Memory 真回放" panel — Baseline uses the
            stored file content, Candidate uses the workspace draft, both
            share the rest of a Source Session's context.
            """
            _session_user(request)
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Memory replay body must be an object",
                )
            raw_checklist = body.get("checklist")
            if isinstance(raw_checklist, str):
                checklist = [
                    line.strip()
                    for line in raw_checklist.splitlines()
                    if line.strip()
                ]
            elif isinstance(raw_checklist, list):
                checklist = list(raw_checklist)
            else:
                checklist = []
            try:
                return await asyncio.to_thread(
                    owner._run_dreamcycle_memory_replay_adhoc,
                    memory_path=str(body.get("memory_path") or ""),
                    before_content=str(body.get("before_content") or ""),
                    after_content=str(body.get("after_content") or ""),
                    query=str(body.get("query") or ""),
                    checklist=checklist,
                    scope=str(body.get("scope") or "team_memory"),
                    source_session_id=str(
                        body.get("source_session_id") or ""
                    ),
                    max_interactions=int(
                        body.get("max_interactions") or 4
                    ),
                    timeout_seconds=int(
                        body.get("timeout_seconds") or 600
                    ),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=str(exc),
                ) from exc

        @app.post("/trigger-dreamcycle/reset")
        async def dreamcycle_reset(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(
                    status_code=400,
                    detail="DreamCycle reset body must be an object",
                )
            remote = bool(body.get("remote", False))
            dry_run = bool(body.get("dry_run", True))
            if not dry_run:
                expected = (
                    "ARCHIVE_REMOTE_MEMORY"
                    if remote
                    else "RESET_LOCAL_STATE"
                )
                if str(body.get("confirmation") or "") != expected:
                    raise HTTPException(
                        status_code=400,
                        detail=f"confirmation must equal {expected}",
                    )
            result = owner._dreamcycle_reset(
                remote=remote,
                dry_run=dry_run,
            )
            if result.get("status") == "running":
                return JSONResponse(content=result, status_code=409)
            return result

        @app.get("/storage/status")
        async def storage_status():
            return JSONResponse(content=_storage_status(owner.config))

        @app.get("/api/sharing-config")
        async def api_get_sharing_config():
            data = ConfigStore().load()
            sharing = data.get("sharing", {}) if isinstance(data.get("sharing"), dict) else {}
            deployment = str(sharing.get("viking_deployment") or "cloud").strip().lower()
            if deployment not in {"cloud", "local"}:
                deployment = "cloud"
            return JSONResponse(
                content={
                    "enabled": bool(sharing.get("enabled", True)),
                    "backend": "viking",
                    "deployment": deployment,
                    "endpoint": resolve_viking_endpoint(
                        deployment, str(sharing.get("viking_endpoint", "") or "")
                    ),
                    "endpoint_override": str(sharing.get("viking_endpoint", "") or ""),
                    "cloud_endpoint": VOLCENGINE_OPENVIKING_ENDPOINT,
                    "local_endpoint": LOCAL_OPENVIKING_ENDPOINT,
                    "account": str(sharing.get("viking_account") or "default"),
                    "personal_user": str(
                        sharing.get("viking_personal_user") or ""
                    ),
                    "team_user": str(sharing.get("viking_user") or "team"),
                    "root_prefix": str(
                        sharing.get("viking_root_prefix") or "team-skill-evolver"
                    ),
                    "team_api_key_present": bool(sharing.get("viking_team_api_key") or sharing.get("viking_api_key")),
                    "personal_api_key_present": bool(sharing.get("viking_personal_api_key")),
                }
            )

        @app.post("/api/sharing-config")
        async def api_save_sharing_config(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="sharing settings body must be an object")
            deployment = str(body.get("deployment") or "cloud").strip().lower()
            if deployment not in {"cloud", "local"}:
                raise HTTPException(
                    status_code=400, detail="deployment must be 'cloud' or 'local'"
                )
            store = ConfigStore()
            data = store.load()
            sharing = data.setdefault("sharing", {})
            sharing["enabled"] = bool(body.get("enabled", sharing.get("enabled", True)))
            sharing["backend"] = "viking"
            sharing["viking_deployment"] = deployment
            # An explicit endpoint override is optional; empty means "derive
            # from the cloud/local deployment default".
            if "endpoint_override" in body:
                sharing["viking_endpoint"] = str(body.get("endpoint_override") or "").strip()
            if "account" in body:
                sharing["viking_account"] = str(body.get("account") or "default").strip()
            if "personal_user" in body:
                sharing["viking_personal_user"] = str(
                    body.get("personal_user") or ""
                ).strip()
            if "team_user" in body:
                sharing["viking_user"] = str(
                    body.get("team_user") or "default"
                ).strip()
            if "root_prefix" in body:
                sharing["viking_root_prefix"] = str(
                    body.get("root_prefix") or "team-skill-evolver"
                ).strip().strip("/")
            if "personal_api_key" in body:
                sharing["viking_personal_api_key"] = str(
                    body.get("personal_api_key") or ""
                ).strip()
            if "team_api_key" in body:
                sharing["viking_team_api_key"] = str(
                    body.get("team_api_key") or ""
                ).strip()
            store.save(data)
            owner.config = store.to_config()
            await owner._reload_openviking_integrations(owner.config)
            return JSONResponse(content=_storage_status(owner.config))

        @app.get("/status")
        async def dashboard_status(refresh: bool = False):
            cache_key = f"status:{id(owner.config)}"
            if refresh:
                _invalidate_dashboard_cache(cache_key)

            def build_status():
                skills: dict[str, dict[str, Any]] = {}
                session_queue = _session_queue_snapshot(owner.config, limit=0)
                try:
                    hub = SkillHub.team_from_config(owner.config)
                    for item in hub.list_remote():
                        name = str(item.get("name") or "")
                        if not name:
                            continue
                        skills[name] = {
                            "skill_id": item.get("skill_id") or name,
                            "version": item.get("version") or 0,
                        }
                except Exception:
                    pass
                if not skills and owner.skill_manager is not None:
                    for skill in owner.skill_manager.get_all_skills():
                        name = str(skill.get("name") or "")
                        if name:
                            skills[name] = {"skill_id": name, "version": 0}
                return {
                    "running": False,
                    "pending_sessions": int(session_queue.get("pending") or 0),
                    "registered_skills": len(skills),
                    "skills": skills,
                }

            return _cached_dashboard_value(cache_key, 5.0, build_status)

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
            rows = _cached_dashboard_value(
                cache_key,
                5.0,
                lambda: SessionStore.from_config(owner.config).list_queue(limit=100000),
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
        ):
            try:
                safe_limit = min(200, max(1, int(limit or 20)))
                safe_offset = max(0, int(offset or 0))
                cache_key = f"conversations:{id(owner.config)}"
                if refresh:
                    _invalidate_dashboard_cache(cache_key)
                conversations = _cached_dashboard_value(
                    cache_key,
                    15.0,
                    lambda: SessionStore.from_config(owner.config).list_conversations(
                        limit=100000
                    ),
                )
                page = conversations[safe_offset : safe_offset + safe_limit]
                return {
                    "reachable": True,
                    "conversations": page,
                    "total": len(conversations),
                    "limit": safe_limit,
                    "offset": safe_offset,
                    "has_more": safe_offset + len(page) < len(conversations),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "reachable": False,
                    "conversations": [],
                    "total": 0,
                    "reason": str(exc),
                }

        @app.post("/conversations/status")
        async def dashboard_conversation_statuses(request: Request):
            body = await request.json()
            raw_ids = body.get("session_ids") if isinstance(body, dict) else []
            session_ids = [
                _safe_session_id(value)
                for value in (raw_ids if isinstance(raw_ids, list) else [])[:500]
            ]
            try:
                store = SessionStore.from_config(owner.config)
                return {
                    "reachable": True,
                    "statuses": store.conversation_statuses(session_ids),
                }
            except Exception as exc:  # noqa: BLE001
                return {"reachable": False, "statuses": {}, "reason": str(exc)}

        @app.get("/conversations/{session_id}")
        async def dashboard_conversation_detail(session_id: str):
            try:
                store = SessionStore.from_config(owner.config)
                session = store.load_session(_safe_session_id(session_id))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
            return _session_detail_payload(session)

        @app.get("/conversations/{session_id}/process")
        async def dashboard_conversation_process(session_id: str):
            cycles = _history_cycles(
                owner.config,
                limit=50,
                session_id=_safe_session_id(session_id),
            )
            return {"cycles": cycles}

        @app.get("/history")
        async def dashboard_history(limit: int = 50, session_id: str = ""):
            return {
                "cycles": _history_cycles(
                    owner.config,
                    limit=max(1, int(limit or 50)),
                    session_id=_safe_session_id(session_id) if session_id else "",
                )
            }

        @app.get("/api/session-filter/audit")
        async def api_session_filter_audit(limit: int = 100, decision: str = ""):
            safe_limit = max(1, int(limit or 100))
            wanted = str(decision or "").strip().lower()
            cache_key = f"session-filter-audit:{id(owner.config)}:{safe_limit}:{wanted}"

            def load_audit() -> dict[str, Any]:
                store = SessionStore.from_config(owner.config)
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
                store = ValidationStore.from_config(owner.config)
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
                store = ValidationStore.from_config(owner.config)
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
                store = ValidationStore.from_config(owner.config)
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
            return ValidationStore.from_config(owner.config)

        def _validation_candidate_detail_payload(store: ValidationStore, job_id: str) -> dict[str, Any]:
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            evaluation = store.load_evaluation(job_id)
            decision = store.load_decision(job_id)
            return {**_candidate_payload(job, evaluation, decision), **_skill_diff_payload(job, owner.config)}

        @app.get("/api/validation/candidates")
        async def api_validation_candidates(
            scope: str = "open",
            limit: int = 20,
            offset: int = 0,
            refresh: bool = False,
            compact: bool = False,
        ):
            try:
                store = _validation_store()
                safe_limit = min(200, max(1, int(limit or 20)))
                safe_offset = max(0, int(offset or 0))
                cache_key = f"candidates:{id(owner.config)}:{scope}"
                if refresh:
                    _invalidate_dashboard_cache(cache_key)
                candidates = _cached_dashboard_value(
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
            if compact:
                page = [_compact_candidate_payload(item) for item in page]
            return {
                "candidates": page,
                "total": len(candidates),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": safe_offset + len(page) < len(candidates),
            }

        @app.get("/api/validation/candidates/{job_id}/detail")
        async def api_validation_candidate_detail(job_id: str):
            store = _validation_store()
            return _validation_candidate_detail_payload(store, job_id)

        @app.post("/api/validation/candidates/{job_id}/evaluate")
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

        @app.post("/api/validation/candidates/{job_id}/validate")
        async def api_validation_candidate_validate(job_id: str, request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            mode = str(body.get("mode") or "auto")
            store = _validation_store()
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
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
            from ..skills.bundle import coerce_skill_bundle
            from ..skills.editor import save_skill

            name = str(candidate_skill.get("name") or "")
            current = job.get("current_skill") if isinstance(job.get("current_skill"), dict) else None
            created = current is None
            result = save_skill(
                owner.config.skills_dir,
                name=name,
                description=str(candidate_skill.get("description") or ""),
                category=str(candidate_skill.get("category") or "general"),
                body=str(candidate_skill.get("content") or ""),
                skill_md="",
            )
            bundle_files = candidate_skill.get("bundle_files")
            if isinstance(bundle_files, dict):
                from ..skills.bundle import write_skill_bundle

                write_skill_bundle(
                    os.path.join(owner.config.skills_dir, name),
                    coerce_skill_bundle({"SKILL.md": build_skill_md(candidate_skill), **bundle_files}),
                    clean=True,
                )
            loaded = owner._reload_skill_manager()
            cloud = owner._cloud_sync_push(name)
            decision = {
                "status": "published",
                "accepted": True,
                "job_id": job_id,
                "skill_name": name,
                "created": created,
                "version": cloud.get("version") or result.get("version"),
                "loaded_skills": loaded,
                "cloud": cloud,
                "evaluation": evaluation,
            }
            store.save_decision(job_id, decision)
            _invalidate_dashboard_cache(
                f"candidates:{id(owner.config)}",
                f"status:{id(owner.config)}",
            )
            return decision

        @app.delete("/api/validation/candidates/{job_id}")
        async def api_validation_candidate_delete(job_id: str, request: Request):
            _require_admin_user(_session_user(request))
            store = _validation_store()
            result = store.delete_job(job_id)
            _invalidate_dashboard_cache(f"candidates:{id(owner.config)}")
            return result

        @app.post("/internal/reload-skills")
        async def reload_skills(
            request: Request,
        ):
            owner = request.app.state.owner
            await owner._pull_skills_from_cloud()
            skill_count = len(owner.skill_manager.get_all_skills()) if owner.skill_manager else 0
            return {"ok": True, "skills": skill_count}

        return app
