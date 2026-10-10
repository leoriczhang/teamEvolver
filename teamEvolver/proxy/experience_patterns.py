"""Tenant-scoped API for OpenViking-backed Skill experience patterns."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request

from team_skills.evolution.experience_library import ExperienceLibraryStore
from teamEvolver.llm import AsyncLLMClient
from team_skills.evolution.patterns import (
    ExperiencePatternCompiler,
    ExperiencePatternStore,
    PatternStoreUnavailable,
)
from teamEvolver.config import (
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_QUEUE_CAPACITY,
    DEFAULT_LLM_TEMPERATURE,
)
from teamEvolver.session_store import SessionStore
from teamEvolver.tenants.registry import current_tenant_id

LOG = logging.getLogger(__name__)


def _library(config: Any) -> ExperienceLibraryStore:
    tenant_id = current_tenant_id()
    sessions = SessionStore.from_config(config, tenant_id=tenant_id)
    return ExperienceLibraryStore(sessions)


def _standalone_llm(config: Any) -> AsyncLLMClient:
    return AsyncLLMClient(
        api_key=str(getattr(config, "llm_api_key", "") or ""),
        base_url=str(getattr(config, "llm_api_base", "") or ""),
        model=str(getattr(config, "llm_model_id", "") or ""),
        max_tokens=int(
            getattr(config, "llm_max_tokens", DEFAULT_LLM_MAX_TOKENS)
            or DEFAULT_LLM_MAX_TOKENS
        ),
        temperature=float(
            getattr(config, "llm_temperature", DEFAULT_LLM_TEMPERATURE)
            if getattr(config, "llm_temperature", None) is not None
            else DEFAULT_LLM_TEMPERATURE
        ),
        tenant_id=current_tenant_id(),
        max_concurrency=int(
            getattr(
                config,
                "llm_max_concurrency",
                DEFAULT_LLM_MAX_CONCURRENCY,
            )
            or DEFAULT_LLM_MAX_CONCURRENCY
        ),
        queue_capacity=int(
            getattr(
                config,
                "llm_queue_capacity",
                DEFAULT_LLM_QUEUE_CAPACITY,
            )
            or DEFAULT_LLM_QUEUE_CAPACITY
        ),
    )


async def _tenant_engine(owner: Any) -> Any | None:
    getter = getattr(owner, "_get_embedded_evolve_server", None)
    if not callable(getter):
        return None
    return await asyncio.to_thread(getter, current_tenant_id())


@asynccontextmanager
async def _engine_lock(engine: Any | None):
    if engine is None or not hasattr(engine, "_get_run_lock"):
        yield
        return
    async with engine._get_run_lock():
        yield


def record_candidate_outcome(
    config: Any,
    job: dict[str, Any],
    *,
    status: str,
    version: int | None = None,
    reason: str = "",
) -> int:
    """Best-effort bridge used by the proxy-owned candidate review routes."""
    refs = [
        dict(item)
        for item in job.get("pattern_refs") or []
        if isinstance(item, dict)
    ]
    if not refs:
        return 0
    try:
        return ExperiencePatternStore.from_config(config).record_candidate_outcome(
            refs,
            job_id=str(job.get("job_id") or ""),
            status=status,
            candidate_revision=max(1, int(job.get("candidate_revision") or 1)),
            version=version,
            reason=reason,
        )
    except Exception:
        LOG.warning(
            "failed to record Pattern candidate outcome job=%s status=%s",
            job.get("job_id"),
            status,
            exc_info=True,
        )
        return 0


def install(
    app: Any,
    owner: Any,
    config_getter: Callable[[Any], Any],
    admin_guard: Callable[[dict[str, Any] | None], None],
    user_resolver: Callable[[Request], Awaitable[dict[str, Any] | None]],
) -> None:
    @app.get("/api/skill-experience-patterns")
    async def list_skill_experience_patterns(
        limit: int = 50,
        offset: int = 0,
        kind: str = "",
        skill: str = "",
        status: str = "",
        search: str = "",
    ):
        wanted_kind = str(kind or "").strip().lower()
        wanted_status = str(status or "").strip().lower()
        if wanted_kind not in {"", "defect", "exemplary"}:
            raise HTTPException(status_code=400, detail="invalid pattern kind")
        if wanted_status not in {"", "ready", "observing"}:
            raise HTTPException(status_code=400, detail="invalid pattern status")
        safe_limit = min(200, max(1, int(limit or 50)))
        safe_offset = max(0, int(offset or 0))
        config = config_getter(owner)
        try:
            store = ExperiencePatternStore.from_config(config)
            payload = await asyncio.to_thread(
                store.list_patterns,
                kind=wanted_kind,
                skill=str(skill or "").strip(),
                status=wanted_status,
                search=str(search or "").strip(),
            )
        except PatternStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"OpenViking Pattern store unavailable: {exc}",
            ) from exc
        available_skills = set(payload.get("skill_counts") or {})
        try:
            experience_payload = await asyncio.to_thread(
                lambda: _library(config).list_experiences()
            )
            available_skills.update(
                str(name)
                for name in (experience_payload.get("skill_counts") or {})
                if str(name).strip()
            )
        except Exception:
            LOG.warning(
                "failed to list Experience skills for Pattern compilation",
                exc_info=True,
            )
        rows = payload.get("items") or []
        page = rows[safe_offset : safe_offset + safe_limit]
        return {
            "items": page,
            "stats": payload.get("stats") or {},
            "skill_counts": payload.get("skill_counts") or {},
            "available_skills": sorted(available_skills),
            "location": payload.get("location") or "",
            "total": len(rows),
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": safe_offset + len(page) < len(rows),
        }

    @app.post("/api/skill-experience-patterns/compile")
    async def compile_skill_experience_patterns(request: Request):
        user = await user_resolver(request)
        admin_guard(user)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid request body")
        skill_name = str(body.get("skill") or "").strip()
        if not skill_name:
            raise HTTPException(status_code=400, detail="skill is required")
        config = config_getter(owner)
        try:
            pattern_store = ExperiencePatternStore.from_config(config)
            experience_store = _library(config)
            payload = await asyncio.to_thread(
                experience_store.list_experiences,
                skill=skill_name,
            )
            experiences = payload.get("items") or []
            if not experiences:
                raise HTTPException(
                    status_code=404,
                    detail=f"no experiences found for Skill {skill_name}",
                )
            engine = await _tenant_engine(owner)
            llm = getattr(engine, "_llm", None) or _standalone_llm(config)
            async with _engine_lock(engine):
                result = await ExperiencePatternCompiler(
                    llm,
                    pattern_store,
                ).compile(
                    skill_name,
                    experiences,
                    force=bool(body.get("force")),
                )
        except HTTPException:
            raise
        except PatternStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOG.warning(
                "manual Pattern compilation failed skill=%s",
                skill_name,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail=f"Pattern compilation failed: {exc}",
            ) from exc
        document = result["document"]
        return {
            "compiled": bool(result["compiled"]),
            "skill_name": skill_name,
            "patterns": document.get("patterns") or [],
            "pattern_count": len(document.get("patterns") or []),
            "compiled_at": document.get("compiled_at"),
            "location": pattern_store.location,
        }
