"""FastAPI application and route wiring for the SkillGene service.

``RoutesMixin`` builds the ``FastAPI`` app and its endpoints (console,
health, skill/user admin, model settings, and internal skill reload). Route bodies delegate to the owning
:class:`~skillgene.proxy.server.ProxyServer` instance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .users_admin import (
    _find_user,
    _load_registry,
    _public_user,
    _registry_path,
    _save_registry,
    _upsert_user,
    _verify_password,
)
from ..config_store import ConfigStore
from ..session_filter import SessionValueClassifier
from ..session_store import SessionStore
from ..skills.hub import SkillHub
from ..skills.render import build_skill_md
from ..storage import is_not_found_error
from ..validation.store import ValidationStore
from ..validation.worker import ValidationWorker

logger = logging.getLogger(__name__)
_SESSION_COOKIE = "skillgene_console_session"
_SESSION_TTL_SECONDS = 24 * 60 * 60


def _model_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    llm = store_data.get("llm") if isinstance(store_data.get("llm"), dict) else {}
    api_key = str(getattr(config, "llm_api_key", "") or llm.get("api_key") or "")
    return {
        "provider": str(llm.get("provider") or getattr(config, "llm_provider", "") or "custom"),
        "base_url": str(getattr(config, "llm_api_base", "") or llm.get("api_base") or ""),
        "model": str(getattr(config, "llm_model_id", "") or llm.get("model_id") or ""),
        "max_tokens": int(getattr(config, "llm_max_tokens", 0) or llm.get("max_tokens") or 100000),
        "temperature": float(getattr(config, "llm_temperature", 0.0) if getattr(config, "llm_temperature", None) is not None else llm.get("temperature", 0.4)),
        "api_key_present": bool(api_key),
    }


def _require_admin_user(user: dict | None) -> None:
    if not user or str(user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")


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
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid ingest api key")


def _is_embedded_evolve_path(path: str) -> bool:
    if path in {"/trigger", "/status", "/sessions", "/conversations", "/storage/status", "/trigger-dreamcycle"}:
        return True
    return path.startswith(
        (
            "/conversations/",
            "/storage/",
            "/validation/",
            "/skills/",
            "/trigger-dreamcycle/",
        )
    )


def _max_session_body_bytes() -> int:
    try:
        value = int(os.environ.get("SKILLGENE_MAX_SESSION_BODY_BYTES", str(8 * 1024 * 1024)) or 0)
    except ValueError:
        value = 8 * 1024 * 1024
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


def _candidate_payload(job: dict[str, Any], evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(job)
    name = _candidate_skill_name(payload)
    if name:
        payload["skill_name"] = name
        payload.setdefault("candidate_skill_name", name)
    payload["proposed_action"] = str(payload.get("proposed_action") or payload.get("action") or "")
    if payload.get("rationale"):
        payload["rationale"] = _scrub_legacy_reward_text(payload.get("rationale"))
    if evaluation:
        eval_payload = _evaluation_payload(job, evaluation, cached=True)
        replay_payload = eval_payload.get("replay") if isinstance(eval_payload.get("replay"), dict) else {}
        payload["evaluation"] = eval_payload
        payload["verify_score"] = eval_payload.get("verify_score")
        payload["replay_score"] = eval_payload.get("replay_score")
        payload["baseline_score"] = replay_payload.get("baseline_mean")
        payload["recommended_publish"] = eval_payload.get("recommended_publish")
        payload["evaluation_error"] = replay_payload.get("error")
    return payload


def _normalize_replay_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(case.get("session_id") or ""),
        "turn_num": int(case.get("turn_num", 0) or 0),
        "instruction": str(case.get("instruction") or ""),
        "baseline": {
            "score": case.get("baseline", {}).get("score") if isinstance(case.get("baseline"), dict) else None,
            "response": (
                case.get("baseline", {}).get("final_response")
                or case.get("baseline", {}).get("response_text")
                if isinstance(case.get("baseline"), dict)
                else ""
            ),
            "instruction": str(case.get("instruction") or ""),
            "session_id": str(case.get("session_id") or ""),
            "turn_num": int(case.get("turn_num", 0) or 0),
            "interaction_turns": case.get("baseline", {}).get("interaction_turns") if isinstance(case.get("baseline"), dict) else None,
            "tool_call_count": case.get("baseline", {}).get("tool_call_count") if isinstance(case.get("baseline"), dict) else None,
            "total_tokens": case.get("baseline", {}).get("total_tokens") if isinstance(case.get("baseline"), dict) else None,
        },
        "candidate": {
            "score": case.get("candidate", {}).get("score") if isinstance(case.get("candidate"), dict) else None,
            "response": (
                case.get("candidate", {}).get("final_response")
                or case.get("candidate", {}).get("response_text")
                if isinstance(case.get("candidate"), dict)
                else ""
            ),
            "instruction": str(case.get("instruction") or ""),
            "session_id": str(case.get("session_id") or ""),
            "turn_num": int(case.get("turn_num", 0) or 0),
            "interaction_turns": case.get("candidate", {}).get("interaction_turns") if isinstance(case.get("candidate"), dict) else None,
            "tool_call_count": case.get("candidate", {}).get("tool_call_count") if isinstance(case.get("candidate"), dict) else None,
            "total_tokens": case.get("candidate", {}).get("total_tokens") if isinstance(case.get("candidate"), dict) else None,
        },
    }


def _evaluation_payload(job: dict[str, Any], result: dict[str, Any], *, cached: bool = False) -> dict[str, Any]:
    replay_summary = result.get("replay_summary") if isinstance(result.get("replay_summary"), dict) else {}
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
        error = str(branch.get("error") or "")
        rationale = str(branch.get("rationale") or branch.get("replay_reason") or "")
        display_response = response or error or rationale
        return {
            "score": _first_present(branch, "score", "normalized_score"),
            "response": display_response,
            "error": error,
            "rationale": rationale,
            "instruction": branch.get("instruction") or item.get("instruction") or "",
            "session_id": branch.get("session_id") or item.get("session_id") or "",
            "turn_num": branch.get("turn_num") if branch.get("turn_num") is not None else item.get("turn_num"),
            "interaction_turns": branch.get("interaction_turns"),
            "tool_call_count": branch.get("tool_call_count"),
            "total_tokens": branch.get("total_tokens"),
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
    threshold = result.get("threshold", job.get("min_score", 0.75))
    return {
        "status": "evaluated",
        "skill_name": skill_name,
        "proposed_action": str(job.get("proposed_action") or job.get("action") or ""),
        "verify_score": result.get("score"),
        "replay_score": result.get("score"),
        "recommended_publish": bool(result.get("accepted")),
        "cached": cached,
        "verification": {
            "threshold": threshold,
            "enabled": True,
            "accepted": bool(result.get("accepted")),
            "decision": result.get("decision"),
            "reason": result.get("reason"),
            "checks": result.get("checks", {}),
        },
        "replay": {
            "threshold": threshold,
            "tolerance": replay_summary.get("tolerance"),
            "baseline_mean": replay_summary.get("baseline_mean_score") or replay_summary.get("baseline_mean"),
            "no_regression": result.get("accepted") or (
                isinstance(replay_summary.get("candidate_mean_score"), (int, float))
                and isinstance(replay_summary.get("baseline_mean_score"), (int, float))
                and float(replay_summary.get("candidate_mean_score")) >= float(replay_summary.get("baseline_mean_score"))
            ),
            "cases": normalized_cases,
            "efficiency": replay_summary.get("efficiency") or {},
            "mode": result.get("validator_mode"),
            "error": fallback_reason or replay_summary.get("reason"),
        },
        "candidate_skill": job.get("candidate_skill"),
        "current_skill": job.get("current_skill"),
    }


async def _evaluate_candidate_job(config, owner, job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    try:
        from ..true_replay import evaluate_job

        try:
            replay_timeout = max(10, int(os.environ.get("SKILLGENE_TRUE_REPLAY_TIMEOUT_S", "90")))
        except ValueError:
            replay_timeout = 90
        try:
            max_interactions = max(1, int(os.environ.get("SKILLGENE_TRUE_REPLAY_MAX_INTERACTIONS", "1")))
        except ValueError:
            max_interactions = 1
        replay = await asyncio.to_thread(
            evaluate_job,
            job_id,
            job=job,
            timeout=replay_timeout,
            max_interactions=max_interactions,
        )
        if replay.get("status") == "evaluated":
            return {
                "validator_mode": "true_replay",
                "decision": "accept" if replay.get("accepted") else "reject",
                "accepted": bool(replay.get("accepted")),
                "score": replay.get("score"),
                "threshold": replay.get("threshold"),
                "reason": (
                    f"True Replay score={replay.get('score')}, "
                    f"baseline={replay.get('baseline_mean')}, "
                    f"delta={replay.get('delta')}, quality_ok={replay.get('quality_ok')}"
                ),
                "checks": {
                    "grounded_in_evidence": replay.get("score"),
                    "preserves_existing_value": 1.0 if replay.get("no_regression") else 0.0,
                    "specificity_and_reusability": replay.get("score"),
                    "safe_to_publish": replay.get("score") if replay.get("accepted") else 0.0,
                },
                "replay_summary": {
                    **replay,
                    "baseline_mean": replay.get("baseline_mean"),
                    "candidate_mean_score": replay.get("score"),
                    "baseline_mean_score": replay.get("baseline_mean"),
                    "cases": replay.get("cases") or [],
                },
            }
        logger.info("[Validation] true replay skipped for %s: %s", job_id, replay.get("reason"))
        fallback_reason = replay.get("reason") or replay.get("status") or "true replay skipped"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Validation] true replay failed for %s: %s", job_id, exc)
        fallback_reason = f"true replay failed: {type(exc).__name__}: {exc}"

    worker = ValidationWorker(config, idle_provider=owner)
    try:
        result = await worker._replay_validate_job(job)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Validation] fallback replay failed for %s: %s", job_id, exc)
        threshold = round(float(job.get("min_score", 0.75) or 0.75), 3)
        result = {
            "validator_mode": "failed",
            "decision": "reject",
            "accepted": False,
            "score": None,
            "threshold": threshold,
            "reason": f"Replay evaluation failed after true replay fallback: {type(exc).__name__}: {exc}",
            "checks": {},
            "replay_summary": {
                "reason": f"fallback replay failed: {type(exc).__name__}: {exc}",
                "cases": [],
            },
        }
    result = dict(result)
    result["true_replay_fallback_reason"] = fallback_reason
    return result


def _skill_diff_payload(job: dict[str, Any]) -> dict[str, Any]:
    import difflib

    current = job.get("current_skill") if isinstance(job.get("current_skill"), dict) else None
    candidate = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else None
    current_md = build_skill_md(current).splitlines() if current else []
    candidate_md = build_skill_md(candidate).splitlines() if candidate else []
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
    namespace = "resources" if backend == "viking" else backend or "none"
    api_key_present = bool(
        str(getattr(config, "sharing_viking_team_api_key", "") or "")
        or str(getattr(config, "sharing_viking_api_key", "") or "")
    )
    payload: dict[str, Any] = {
        "backend": backend or "none",
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


class RoutesMixin:
    """FastAPI app construction, routing, and request authentication."""

    def _build_app(self) -> FastAPI:
        owner = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            owner._ready_event.set()
            owner._start_skill_reload_polling()
            owner._start_embedded_evolve()
            try:
                yield
            finally:
                owner._ready_event.clear()
                await owner._shutdown_cleanup()

        app = FastAPI(title="SkillGene", lifespan=lifespan)
        app.state.owner = self
        self._console_sessions = getattr(self, "_console_sessions", {})
        dist_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "dist"))
        dist_index = os.path.join(dist_dir, "index.html")
        dist_assets = os.path.join(dist_dir, "assets")
        if os.path.isdir(dist_assets):
            app.mount("/assets", StaticFiles(directory=dist_assets), name="assets")

        def _session_user(request: Request) -> dict | None:
            token = request.cookies.get(_SESSION_COOKIE, "")
            if not token:
                return None
            session = owner._console_sessions.get(token)
            if not isinstance(session, dict):
                return None
            if float(session.get("expires_at", 0) or 0) < time.time():
                owner._console_sessions.pop(token, None)
                return None
            user_id = str(session.get("user_id") or "")
            if not user_id:
                return None
            data = _load_registry(_registry_path(owner.config))
            try:
                _idx, user = _find_user(data, user_id)
            except HTTPException:
                owner._console_sessions.pop(token, None)
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
        self._register_users_admin_routes(app)

        @app.get("/")
        @app.get("/console")
        async def console():
            if os.path.isfile(dist_index):
                return FileResponse(dist_index)
            return JSONResponse(status_code=404, content={"detail": "SkillGene console is not built"})

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
            user = _upsert_user(data, payload)
            _save_registry(path, data)
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            resp = JSONResponse(content={"authenticated": True, "needs_setup": False, "user": _public_user(user, owner.config)})
            resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=_SESSION_TTL_SECONDS, path="/")
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
            resp = JSONResponse(content={"authenticated": True, "needs_setup": False, "user": _public_user(user, owner.config)})
            resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=_SESSION_TTL_SECONDS, path="/")
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
            user = _upsert_user(data, payload)
            _save_registry(path, data)
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            resp = JSONResponse(content={"authenticated": True, "needs_setup": False, "user": _public_user(user, owner.config)})
            resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=_SESSION_TTL_SECONDS, path="/")
            return resp

        @app.post("/api/auth/logout")
        async def auth_logout(request: Request):
            token = request.cookies.get(_SESSION_COOKIE, "")
            if token:
                owner._console_sessions.pop(token, None)
            resp = JSONResponse(content={"authenticated": False})
            resp.delete_cookie(_SESSION_COOKIE, path="/")
            return resp

        @app.get("/api/evolve-model")
        async def api_get_evolve_model():
            store = ConfigStore()
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

            store = ConfigStore()
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
            owner.config = store.to_config()
            return JSONResponse(content=_model_settings_payload(owner.config, data))

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
            api_key = str(raw_key).strip() if raw_key is not None and str(raw_key).strip() else str(owner.config.llm_api_key or llm.get("api_key") or "")
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
            _check_ingest_api_key(request)
            body = await _read_limited_json_body(request)
            session_id = _safe_session_id(body.get("session_id"))
            session = dict(body)
            session["session_id"] = session_id
            session.setdefault("user_alias", str(getattr(owner.config, "sharing_user_alias", "") or "anonymous"))

            classifier = SessionValueClassifier.from_config(owner.config)
            value_judge = await classifier.classify(session)
            session["value_judge"] = value_judge
            session["ingested_at"] = _utc_now_iso()
            try:
                session_store = SessionStore.from_config(owner.config)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=503, detail="session storage is not configured") from exc

            if value_judge.get("decision") != "valuable":
                session_store.save_skipped(session)
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
            trigger_scheduled = owner._schedule_evolve_trigger()
            logger.info("[SessionFilter] queued valuable session=%s key=%s", session_id, key)
            return {
                "status": "queued",
                "session_id": session_id,
                "queued": True,
                "key": key,
                "trigger_scheduled": trigger_scheduled,
                "value_judge": value_judge,
            }

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/storage/status")
        async def storage_status():
            return JSONResponse(content=_storage_status(owner.config))

        @app.get("/status")
        async def dashboard_status():
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

        @app.get("/sessions")
        async def dashboard_sessions():
            snapshot = _session_queue_snapshot(owner.config)
            return {
                "reachable": bool(snapshot.get("reachable")),
                "sessions": snapshot.get("sessions", []),
                "pending": int(snapshot.get("pending") or 0),
                **({"reason": snapshot.get("reason")} if snapshot.get("reason") else {}),
            }

        @app.get("/conversations")
        async def dashboard_conversations(limit: int = 100):
            try:
                store = SessionStore.from_config(owner.config)
                conversations = store.list_conversations(limit=max(1, int(limit or 100)))
                return {"reachable": True, "conversations": conversations}
            except Exception as exc:  # noqa: BLE001
                return {"reachable": False, "conversations": [], "reason": str(exc)}

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
            cycles = _history_from_archived_sessions(
                owner.config,
                limit=50,
                session_id=_safe_session_id(session_id),
            )
            return {"cycles": cycles}

        @app.get("/history")
        async def dashboard_history(limit: int = 50, session_id: str = ""):
            return {
                "cycles": _history_from_archived_sessions(
                    owner.config,
                    limit=max(1, int(limit or 50)),
                    session_id=_safe_session_id(session_id) if session_id else "",
                )
            }

        @app.get("/api/session-filter/audit")
        async def api_session_filter_audit(limit: int = 100, decision: str = ""):
            try:
                store = SessionStore.from_config(owner.config)
                return {
                    "stats": store.filter_stats(),
                    "items": store.list_filter_audit(
                        limit=max(1, int(limit or 100)),
                        decision=decision,
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                return {"stats": {"total": 0, "decisions": {}, "statuses": {}, "modes": {}}, "items": [], "reason": str(exc)}

        @app.get("/validation/candidates")
        async def validation_candidates():
            try:
                store = ValidationStore.from_config(owner.config)
                candidates = []
                for job in store.list_open_jobs(user_alias=str(owner.config.sharing_user_alias or "")):
                    job_id = str(job.get("job_id") or "")
                    evaluation = store.load_evaluation(job_id) if job_id else None
                    candidates.append(_candidate_payload(job, evaluation))
            except Exception:
                candidates = []
            return {"candidates": candidates}

        @app.get("/validation/candidates/{job_id}")
        async def validation_candidate_detail(job_id: str):
            store = ValidationStore.from_config(owner.config)
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            return {**_candidate_payload(job), **_skill_diff_payload(job)}

        @app.post("/validation/candidates/{job_id}/evaluate")
        async def validation_candidate_evaluate(job_id: str, refresh: bool = False):
            store = ValidationStore.from_config(owner.config)
            job = store.load_job(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            cached = None if refresh else store.load_evaluation(job_id)
            if cached:
                return _evaluation_payload(job, cached, cached=True)
            result = await _evaluate_candidate_job(owner.config, owner, job)
            store.save_evaluation(job_id, result)
            return _evaluation_payload(job, result, cached=False)

        @app.post("/validation/candidates/{job_id}/validate")
        async def validation_candidate_validate(job_id: str, request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            mode = str(body.get("mode") or "auto")
            store = ValidationStore.from_config(owner.config)
            job = store.load_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="candidate not found")
            evaluation = store.load_evaluation(job_id)
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
            return decision

        @app.delete("/validation/candidates/{job_id}")
        async def validation_candidate_delete(job_id: str, request: Request):
            _require_admin_user(_session_user(request))
            store = ValidationStore.from_config(owner.config)
            return store.delete_job(job_id)

        @app.post("/internal/reload-skills")
        async def reload_skills(
            request: Request,
        ):
            owner = request.app.state.owner
            await owner._pull_skills_from_cloud()
            skill_count = len(owner.skill_manager.get_all_skills()) if owner.skill_manager else 0
            return {"ok": True, "skills": skill_count}

        return app
