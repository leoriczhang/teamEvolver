"""Generic Skill publish callback delivery for registered Agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..validation.runtime_compatibility import skill_supports_runtime
from .agent_protocol import CAP_SKILL_SYNC
from .agent_registry import list_agents


def _sync_api_key(
    profile: str,
    *,
    legacy_agentshub_key: str = "",
) -> str:
    normalized = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        str(profile or "").strip(),
    ).strip("_").upper()
    if normalized:
        value = str(
            os.environ.get(
                f"TEAMEVOLVER_AGENT_{normalized}_SKILL_SYNC_API_KEY",
                "",
            )
            or ""
        ).strip()
        if value:
            return value
    return str(legacy_agentshub_key or "").strip()


def _target_tenant_ids(
    agent: dict[str, Any],
    requested: list[str],
) -> list[str]:
    metadata = (
        agent.get("metadata")
        if isinstance(agent.get("metadata"), dict)
        else {}
    )
    agent_tenant = str(metadata.get("tenant_id") or "").strip()
    requested = [
        str(item or "").strip()
        for item in requested
        if str(item or "").strip()
    ]
    if agent_tenant:
        if requested and agent_tenant not in requested:
            return []
        return [agent_tenant]
    return list(dict.fromkeys(requested))


def _ack_matches(
    body: dict[str, Any],
    *,
    action: str,
    skills: list[dict[str, Any]],
    tenant_ids: list[str],
) -> tuple[bool, str]:
    if body.get("ok") is not True:
        return False, "callback did not return ok=true"
    results = body.get("results")
    if not isinstance(results, dict):
        return False, "callback did not return per-tenant verification"
    for tenant_id in tenant_ids:
        tenant = results.get(tenant_id)
        verification = (
            tenant.get("verification")
            if isinstance(tenant, dict)
            and isinstance(tenant.get("verification"), dict)
            else {}
        )
        checks = {
            str(item.get("name") or ""): item
            for item in verification.get("skills") or []
            if isinstance(item, dict)
        }
        for expected in skills:
            name = str(expected.get("name") or "")
            check = checks.get(name, {})
            if action == "delete":
                if not bool(check.get("matched")) or not bool(
                    check.get("removed")
                ):
                    return False, f"{tenant_id}:{name} removal was not acknowledged"
                continue
            if not bool(check.get("matched")):
                return False, f"{tenant_id}:{name} version/hash mismatch"
            if int(check.get("actual_version") or 0) != int(
                expected.get("version") or 0
            ):
                return False, f"{tenant_id}:{name} version mismatch"
            if str(check.get("actual_sha256") or "") != str(
                expected.get("sha256") or ""
            ):
                return False, f"{tenant_id}:{name} sha256 mismatch"
            expected_tree = str(expected.get("tree_sha256") or "")
            if expected_tree and str(
                check.get("actual_tree_sha256") or ""
            ) != expected_tree:
                return False, f"{tenant_id}:{name} tree_sha256 mismatch"
    return True, ""


def _delivery_due(delivery: dict[str, Any]) -> bool:
    try:
        value = datetime.fromisoformat(str(delivery.get("next_retry_at") or ""))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


async def sync_skill_event(
    config: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or "")
    action = str(event.get("action") or "publish")
    skills = [
        dict(item)
        for item in event.get("skills") or []
        if isinstance(item, dict)
    ]
    tenant_ids = list(event.get("tenant_ids") or [])
    mutation_id = str(event.get("mutation_id") or "")
    if not event_id or not skills:
        raise ValueError("skill sync event_id and skills are required")
    results: dict[str, Any] = {}
    known_agents: set[str] = set()
    persisted = (
        event.get("deliveries")
        if isinstance(event.get("deliveries"), dict)
        else {}
    )
    for agent in list_agents(config):
        agent_id = str(agent.get("agent_id") or "")
        known_agents.add(agent_id)
        capabilities = set(agent.get("capability_ids") or [])
        legacy = set(agent.get("capabilities") or [])
        if CAP_SKILL_SYNC not in capabilities and "skill_sync" not in legacy:
            if agent_id in persisted:
                results[agent_id] = {
                    "status": "cancelled",
                    "reason": "Agent no longer declares skill.sync.v1",
                    "attempted": False,
                }
            continue
        previous = (
            persisted.get(agent_id)
            if isinstance(persisted.get(agent_id), dict)
            else {}
        )
        if previous.get("status") in {"synced", "cancelled"}:
            continue
        if previous and not _delivery_due(previous):
            results[agent_id] = {
                **previous,
                "status": "pending",
                "attempted": False,
            }
            continue
        if str(agent.get("status") or "active") != "active":
            results[agent_id] = {
                "status": "cancelled",
                "reason": "Agent integration is disabled",
                "attempted": False,
            }
            continue
        agent_skills = [
            skill
            for skill in skills
            if skill_supports_runtime(
                skill,
                str(
                    agent.get("runtime_class")
                    or agent.get("runtime_type")
                    or ""
                ),
            )
        ]
        if not agent_skills:
            results[agent_id] = {
                "status": "cancelled",
                "reason": "Skill does not support this runtime class",
                "attempted": False,
            }
            continue
        delivery_tenants = _target_tenant_ids(agent, tenant_ids)
        if tenant_ids and not delivery_tenants:
            continue
        endpoints = (
            agent.get("endpoints")
            if isinstance(agent.get("endpoints"), dict)
            else {}
        )
        endpoint = str(endpoints.get("skill_sync_url") or "").strip()
        if not endpoint:
            results[agent_id] = {
                "status": "failed",
                "reason": "skill_sync_url is not configured",
                "attempted": True,
            }
            continue
        details = (
            agent.get("capability_details", {}).get(CAP_SKILL_SYNC)
            if isinstance(agent.get("capability_details"), dict)
            else {}
        )
        profile = str(
            (details or {}).get("auth_profile")
            or (agent.get("auth") or {}).get("replay_profile")
            or ""
        )
        key = _sync_api_key(
            profile,
            legacy_agentshub_key=(
                str(getattr(config, "validation_agentshub_api_key", "") or "")
                if str(agent.get("runtime_type") or "") == "agentshub"
                else ""
            ),
        )
        headers = {"Idempotency-Key": f"{event_id}:{agent_id}"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "schema_version": "teamevolver.skill-changed.v1",
            "protocol_version": "1.0",
            "event_id": event_id,
            "action": action,
            "job_id": mutation_id,
            "skills": agent_skills,
            # Compatibility fields for the current AgentsHub callback.
            "tenant_ids": delivery_tenants,
            "expected_skills": agent_skills,
        }
        try:
            async with httpx.AsyncClient(
                timeout=120.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("callback response must be an object")
            matched, reason = _ack_matches(
                body,
                action=action,
                skills=agent_skills,
                tenant_ids=delivery_tenants,
            )
            if not matched:
                raise RuntimeError(reason)
            results[agent_id] = {
                "status": "synced",
                "ack": body,
                "attempted": True,
            }
        except Exception as exc:
            results[agent_id] = {
                "status": "failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "attempted": True,
            }
    for agent_id in set(event.get("deliveries") or {}) - known_agents:
        results[agent_id] = {
            "status": "cancelled",
            "reason": "Agent integration is no longer registered",
            "attempted": False,
        }
    return {
        "event_id": event_id,
        "results": results,
        "status": (
            "synced"
            if results and all(
                item.get("status") in {"synced", "cancelled"}
                for item in results.values()
            )
            else "failed"
            if results
            else "no_capable_agents"
        ),
    }


async def sync_published_skill(
    config: Any,
    *,
    job_id: str,
    expected: dict[str, Any],
    tenant_ids: list[str],
) -> dict[str, Any]:
    event_id = "skill_evt_" + hashlib.sha256(
        json.dumps(
            {
                "job_id": job_id,
                "action": "publish",
                "expected": expected,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:32]
    return await sync_skill_event(
        config,
        {
            "event_id": event_id,
            "mutation_id": job_id,
            "action": "publish",
            "skills": [expected],
            "tenant_ids": tenant_ids,
        },
    )
