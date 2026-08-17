"""Persistent registry for Agent runtimes connected to teamEvolver."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_protocol import (
    CAP_CONTEXT_WORKSPACE,
    CAP_MEMORY_PERSONAL_WRITE,
    CAP_REPLAY_BRANCH,
    CAP_SESSION_INGEST,
    normalize_registration,
)

_DEFAULT_REGISTRY_PATH = Path.home() / ".teamEvolver" / "agents.json"
_SECRET_TOKENS = ("key", "token", "secret", "password", "credential")
_ENDPOINT_FIELDS = {
    "health_url",
    "replay_url",
    "skill_sync_url",
    "session_ingest_url",
}
_REGISTRY_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _registry_path(config) -> Path:
    users_path = str(getattr(config, "users_registry_path", "") or "").strip()
    if users_path:
        return Path(users_path).expanduser().parent / "agents.json"
    config_file = str(getattr(config, "_config_file", "") or "").strip()
    if config_file:
        return Path(config_file).expanduser().parent / "agents.json"
    return _DEFAULT_REGISTRY_PATH


def _safe_id(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip())
    normalized = normalized.strip(".:-")
    if not normalized:
        raise ValueError("agent_id is required")
    return normalized[:160]


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()
            if not key or any(token in key.lower() for token in _SECRET_TOKENS):
                continue
            safe = _safe_value(raw_value, depth=depth + 1)
            if safe is not None:
                result[key] = safe
        return result
    if isinstance(value, list):
        return [
            safe
            for item in value[:100]
            if (safe := _safe_value(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _safe_mapping(value: Any) -> dict[str, Any]:
    result = _safe_value(value)
    return result if isinstance(result, dict) else {}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"agents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {"agents": []}
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return {"agents": []}
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def register_agent(config, payload: dict[str, Any]) -> dict[str, Any]:
    """Register one runtime without persisting credentials."""
    normalized = normalize_registration(payload)
    agent_id = _safe_id(normalized.get("agent_id"))
    runtime_type = _safe_id(
        normalized.get("runtime_type") or agent_id.split(":", 1)[0]
    )
    capabilities = sorted(
        {
            str(item or "").strip()
            for item in normalized.get("capabilities") or []
            if str(item or "").strip()
        }
    )
    raw_endpoints = (
        normalized.get("endpoints")
        if isinstance(normalized.get("endpoints"), dict)
        else {}
    )
    endpoints = {
        key: str(raw_endpoints.get(key) or "").strip().rstrip("/")
        for key in _ENDPOINT_FIELDS
        if str(raw_endpoints.get(key) or "").strip()
    }
    path = _registry_path(config)
    with _REGISTRY_LOCK:
        data = _load(path)
        existing = next(
            (
                item
                for item in data["agents"]
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == agent_id
            ),
            {},
        )
        record = {
            "schema_version": str(normalized.get("schema_version") or ""),
            "protocol_version": str(normalized.get("protocol_version") or ""),
            "runtime_version": str(normalized.get("runtime_version") or ""),
            "agent_id": agent_id,
            "runtime_type": runtime_type,
            "display_name": str(
                normalized.get("display_name")
                or existing.get("display_name")
                or agent_id
            ),
            "capabilities": capabilities,
            "capability_ids": list(normalized.get("capability_ids") or []),
            "capability_details": _safe_mapping(
                normalized.get("capability_details")
            ),
            "endpoints": endpoints,
            "auth": _safe_mapping(normalized.get("auth")),
            "metadata": _safe_mapping(normalized.get("metadata")),
            "compatibility": str(
                normalized.get("compatibility") or "legacy"
            ),
            "status": "active",
            "created_at": str(existing.get("created_at") or _now()),
            "updated_at": _now(),
        }
        if isinstance(existing.get("access_auth"), dict):
            record["access_auth"] = dict(existing["access_auth"])
        data["agents"] = [
            item
            for item in data["agents"]
            if not isinstance(item, dict)
            or str(item.get("agent_id") or "") != agent_id
        ]
        data["agents"].append(record)
        data["agents"].sort(key=lambda item: str(item.get("agent_id") or ""))
        _save(path, data)
    return record


def list_agents(config) -> list[dict[str, Any]]:
    with _REGISTRY_LOCK:
        return [
            item
            for item in _load(_registry_path(config)).get("agents") or []
            if isinstance(item, dict)
        ]


def public_agent_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in record.items() if key != "access_auth"}
    auth = record.get("access_auth") if isinstance(record.get("access_auth"), dict) else {}
    payload["access_token_configured"] = bool(auth.get("token_sha256"))
    payload["access_scopes"] = list(auth.get("scopes") or [])
    payload["access_token_rotated_at"] = str(auth.get("rotated_at") or "")
    return payload


def _access_scopes(record: dict[str, Any]) -> list[str]:
    capabilities = set(record.get("capability_ids") or [])
    scopes: set[str] = set()
    if CAP_SESSION_INGEST in capabilities:
        scopes.add("session.ingest")
    if CAP_CONTEXT_WORKSPACE in capabilities:
        scopes.update(
            {
                "context.describe",
                "context.resolve",
                "context.read",
                "context.skills",
                "context.session",
            }
        )
    if CAP_MEMORY_PERSONAL_WRITE in capabilities:
        scopes.update({"context.remember", "context.forget"})
    return sorted(scopes)


def issue_agent_access_token(
    config,
    *,
    agent_id: str,
    rotate: bool = False,
) -> tuple[dict[str, Any], str]:
    path = _registry_path(config)
    with _REGISTRY_LOCK:
        data = _load(path)
        record = next(
            (
                item
                for item in data.get("agents") or []
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == str(agent_id or "")
            ),
            None,
        )
        if record is None:
            raise ValueError(f"registered Agent not found: {agent_id}")
        existing = (
            record.get("access_auth")
            if isinstance(record.get("access_auth"), dict)
            else {}
        )
        if existing.get("token_sha256") and not rotate:
            return record, ""
        token = "tev1_" + secrets.token_urlsafe(32)
        record["access_auth"] = {
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "scopes": _access_scopes(record),
            "rotated_at": _now(),
        }
        record["updated_at"] = _now()
        _save(path, data)
        return record, token


def verify_agent_access_token(
    config,
    token: str,
    *,
    required_scope: str = "",
) -> dict[str, Any] | None:
    candidate = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    for record in list_agents(config):
        auth = (
            record.get("access_auth")
            if isinstance(record.get("access_auth"), dict)
            else {}
        )
        expected = str(auth.get("token_sha256") or "")
        if not expected or not hmac.compare_digest(candidate, expected):
            continue
        if str(record.get("status") or "active") != "active":
            return None
        if required_scope and required_scope not in set(auth.get("scopes") or []):
            return None
        return record
    return None


def resolve_runtime_agent(
    config,
    *,
    runtime_type: str,
    agent_id: str = "",
    allow_runtime_fallback: bool = True,
) -> dict[str, Any] | None:
    """Resolve an exact Agent id, optionally falling back to runtime type."""
    records = list_agents(config)
    cleaned_id = str(agent_id or "").strip()
    if cleaned_id:
        exact = next(
            (item for item in records if str(item.get("agent_id") or "") == cleaned_id),
            None,
        )
        if exact or not allow_runtime_fallback:
            return exact
    cleaned_type = str(runtime_type or "").strip()
    matches = [
        item
        for item in records
        if str(item.get("runtime_type") or "") == cleaned_type
    ]
    return max(matches, key=lambda item: str(item.get("updated_at") or "")) if matches else None


def resolve_replay_capability(
    config,
    *,
    runtime_type: str,
    agent_id: str = "",
    allow_runtime_fallback: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    record = resolve_runtime_agent(
        config,
        runtime_type=runtime_type,
        agent_id=agent_id,
        allow_runtime_fallback=allow_runtime_fallback,
    )
    if record is None or str(record.get("status") or "active") != "active":
        return None
    capabilities = set(record.get("capability_ids") or [])
    legacy = set(record.get("capabilities") or [])
    if CAP_REPLAY_BRANCH not in capabilities and "true_replay" not in legacy:
        return None
    details = (
        record.get("capability_details", {}).get(CAP_REPLAY_BRANCH)
        if isinstance(record.get("capability_details"), dict)
        else {}
    )
    detail = dict(details) if isinstance(details, dict) else {}
    endpoints = (
        record.get("endpoints")
        if isinstance(record.get("endpoints"), dict)
        else {}
    )
    detail.setdefault("transport", "http" if endpoints.get("replay_url") else "local")
    if endpoints.get("replay_url"):
        detail.setdefault("endpoint", str(endpoints["replay_url"]))
    detail.setdefault("max_interactions", 20)
    detail.setdefault("idempotent", False)
    return record, detail
