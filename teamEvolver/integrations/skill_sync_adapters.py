"""Generic Skill publish callback delivery for registered Agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

import httpx

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
    results: dict[str, Any] = {}
    for agent in list_agents(config):
        capabilities = set(agent.get("capability_ids") or [])
        legacy = set(agent.get("capabilities") or [])
        if CAP_SKILL_SYNC not in capabilities and "skill_sync" not in legacy:
            continue
        endpoints = (
            agent.get("endpoints")
            if isinstance(agent.get("endpoints"), dict)
            else {}
        )
        endpoint = str(endpoints.get("skill_sync_url") or "").strip()
        if not endpoint:
            results[str(agent.get("agent_id") or "")] = {
                "status": "skipped",
                "reason": "skill_sync_url is not configured",
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
        headers = {"Idempotency-Key": f"{event_id}:{agent.get('agent_id')}"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "schema_version": "teamevolver.skill-changed.v1",
            "protocol_version": "1.0",
            "event_id": event_id,
            "action": "publish",
            "job_id": job_id,
            "skills": [expected],
            # Compatibility fields for the current AgentsHub callback.
            "tenant_ids": tenant_ids,
            "expected_skills": [expected],
        }
        last_error = ""
        for attempt in range(8):
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
                results[str(agent.get("agent_id") or "")] = {
                    "status": "synced",
                    "detail": body,
                }
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 7:
                    await asyncio.sleep(2.0)
        else:
            results[str(agent.get("agent_id") or "")] = {
                "status": "failed",
                "detail": last_error,
            }
    return {
        "event_id": event_id,
        "results": results,
        "status": (
            "synced"
            if results and all(
                item.get("status") in {"synced", "skipped"}
                for item in results.values()
            )
            else "failed"
            if results
            else "no_capable_agents"
        ),
    }
