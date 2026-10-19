"""Validated current Memory task input."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from teamEvolver.security.agent_principal import normalize_user_id

from .openviking import target_uri


def normalize_last_compile_time(value):
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        raise ValueError("invalid last_compile_time")
    if isinstance(value, (float, int)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def run_payload(raw, config, account, user):
    allowed = {"account_id", "user_ids", "kinds", "mode", "target_uri", "pipeline", "last_compile_time"}
    if set(raw) - allowed:
        raise ValueError("unsupported Memory request fields")
    if raw.get("account_id", account) != account:
        raise ValueError("TENANT_ACCOUNT_MISMATCH")
    pipeline = raw.get("pipeline", "both")
    if pipeline not in {"both", "aggregation", "maintenance"}:
        raise ValueError("invalid Memory pipeline")
    users = raw.get("user_ids")
    if users is not None and (not isinstance(users, list) or not users or len(users) > 50_000):
        raise ValueError("user_ids must be a bounded non-empty array")
    if users and len(users) != len(set(users)):
        raise ValueError("user_ids must be unique")
    if raw.get("mode", "incremental") not in {"incremental", "full"}:
        raise ValueError("invalid Memory mode")
    agg = config.config.get("aggregation", config.config)
    kinds = raw.get("kinds") or agg.get("kinds") or []
    if not isinstance(kinds, list) or any(not isinstance(k, str) for k in kinds):
        raise ValueError("kinds must be an array of strings")
    return {
        "account_id": account, "user_id": normalize_user_id(user),
        "user_ids": [normalize_user_id(u) for u in users] if users else None,
        "target_uri": target_uri(raw.get("target_uri") or "viking://resources/" +
                                 str(agg.get("shared_knowledge_prefix", "shared-knowledge"))),
        "kinds": kinds,
        "pipeline": pipeline, "full": raw.get("mode") == "full",
        "last_compile_time": normalize_last_compile_time(raw.get("last_compile_time")),
        "config": copy.deepcopy(config.config), "config_revision": config.revision,
    }
