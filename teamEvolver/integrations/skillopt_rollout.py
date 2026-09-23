"""skillopt rollout: second outbox consumer pushing Skill bundles to DEAP.

After a skill publish commits, the full version bundle is written into a
persistent DEAP workspace (``rollout-<timestamp>-<suffix>``, one per outbox event,
never deleted) via the agent's ``/skillopt/update`` endpoint. The v2
skill-sync delivery in :mod:`skill_sync_adapters` is unaffected; rollout
state lives under ``event["deliveries"]["skillopt_rollout"]``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from team_skills.library.frontmatter import validate_skill_md

from .skill_sync_adapters import SKILLOPT_ROLLOUT_CONSUMER, _delivery_due

logger = logging.getLogger(__name__)

CONSUMER_ID = SKILLOPT_ROLLOUT_CONSUMER
MAX_ATTEMPTS = 8

_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_SUPERVISOR_STATUS: dict[str, Any] = {
    "running": False,
    "updated_at": "",
    "tenants": {},
}


class RolloutDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        failure_kind: str,
        file_path: str = "",
        http_status: int | None = None,
        response_excerpt: str = "",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.failure_kind = failure_kind
        self.file_path = file_path
        self.http_status = http_status
        self.response_excerpt = response_excerpt


def _new_workspace(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}{stamp}-{uuid.uuid4().hex[:12]}"


def update_supervisor_status(
    tenants: dict[str, dict[str, Any]],
    *,
    running: bool = True,
) -> None:
    _SUPERVISOR_STATUS.update(
        {
            "running": running,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "tenants": {key: dict(value) for key, value in tenants.items()},
        }
    )


def supervisor_status() -> dict[str, Any]:
    return {
        "running": bool(_SUPERVISOR_STATUS.get("running")),
        "updated_at": str(_SUPERVISOR_STATUS.get("updated_at") or ""),
        "tenants": {
            key: dict(value)
            for key, value in dict(_SUPERVISOR_STATUS.get("tenants") or {}).items()
        },
    }


def _event_skills(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in event.get("skills") or []
        if isinstance(item, dict)
    ]


def _prepare_posts(service: Any, live: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    posts: list[tuple[str, str, str]] = []
    for skill in live:
        name = str(skill.get("name") or "").strip()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise RolloutDeliveryError(
                f"invalid DEAP skill name: {name!r}",
                retryable=False,
                failure_kind="payload_validation",
            )
        version = int(skill.get("version") or 0)
        try:
            bundle = service.hub.read_version_bundle(name, version)
        except Exception as exc:
            raise RolloutDeliveryError(
                f"version bundle unavailable for {name} v{version}: {exc}",
                retryable=True,
                failure_kind="bundle_read",
            ) from exc
        for rel_path, content in sorted(bundle.items()):
            normalized_path = str(rel_path).replace("\\", "/")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RolloutDeliveryError(
                    f"bundle file is not UTF-8: {name}/{normalized_path}",
                    retryable=False,
                    failure_kind="payload_validation",
                    file_path=f"{name}/{normalized_path}",
                ) from exc
            if normalized_path == "SKILL.md":
                try:
                    validate_skill_md(text, expected_name=name)
                except ValueError as exc:
                    raise RolloutDeliveryError(
                        str(exc),
                        retryable=False,
                        failure_kind="payload_validation",
                        file_path=f"{name}/{normalized_path}",
                    ) from exc
            posts.append((name, normalized_path, text))
    if not posts:
        raise RolloutDeliveryError(
            "no bundle files resolved for rollout",
            retryable=True,
            failure_kind="bundle_read",
        )
    return posts


async def deliver_rollout(
    config: Any,
    service: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Deliver one outbox event to the DEAP /skillopt workspace.

    Returns ``{status: synced|failed|skipped|pending, workspace,
    files_written, file_paths, error, attempted}``.
    """
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return {"status": "skipped", "reason": "missing event_id", "attempted": False}
    if str(event.get("status") or "") == "cancelled":
        return {"status": "skipped", "reason": "event cancelled", "attempted": False}

    deliveries = (
        event.get("deliveries")
        if isinstance(event.get("deliveries"), dict)
        else {}
    )
    previous = dict(deliveries.get(CONSUMER_ID) or {})
    if previous.get("status") == "synced":
        return {
            "status": "skipped",
            "reason": "already synced",
            "workspace": previous.get("workspace"),
            "attempted": False,
        }
    if previous.get("status") == "dead_letter":
        return {
            "status": "skipped",
            "reason": "dead letter",
            "workspace": previous.get("workspace"),
            "attempted": False,
        }
    if previous and not _delivery_due(previous):
        return {
            "status": "pending",
            "reason": "retry not due",
            "workspace": previous.get("workspace"),
            "attempted": False,
        }

    skills = _event_skills(event)
    live = [skill for skill in skills if not bool(skill.get("deleted"))]
    if skills and not live:
        # Delete events never call /skillopt/delete: rollout workspaces are
        # append-only overlays on the original skill directory.
        return {"status": "skipped", "reason": "delete-only event", "attempted": False}

    endpoint = str(
        getattr(config, "skillopt_rollout_endpoint", "") or ""
    ).rstrip("/")
    prefix = str(
        getattr(config, "skillopt_rollout_workspace_prefix", "rollout-")
        or "rollout-"
    )

    workspace = str(previous.get("workspace") or "").strip()
    if not workspace:
        workspace = _new_workspace(prefix)
        # Persist before the first POST so crash retries reuse the workspace
        # (/skillopt/update is an idempotent overlay).
        service.record_delivery(event_id, CONSUMER_ID, {
            "status": "pending",
            "workspace": workspace,
            "attempt": 0,
            "next_retry_at": datetime.now(timezone.utc).isoformat(),
        })

    attempt = int(previous.get("attempt") or 0)
    files_written = 0
    file_paths: list[str] = []
    try:
        posts = _prepare_posts(service, live)

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            for name, rel_path, content in posts:
                response = await client.post(
                    f"{endpoint}/skillopt/update",
                    json={
                        "workspace": workspace,
                        "fileName": f"{name}/{rel_path}",
                        "content": content,
                    },
                )
                if response.status_code >= 400:
                    excerpt = response.text[:1000]
                    status_code = int(response.status_code)
                    raise RolloutDeliveryError(
                        (
                            f"/skillopt/update returned HTTP {status_code} "
                            f"for {name}/{rel_path}: {excerpt}"
                        ),
                        retryable=(
                            status_code in {408, 429} or status_code >= 500
                        ),
                        failure_kind="upstream_http",
                        file_path=f"{name}/{rel_path}",
                        http_status=status_code,
                        response_excerpt=excerpt,
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise RolloutDeliveryError(
                        f"/skillopt/update returned invalid JSON for {name}/{rel_path}",
                        retryable=False,
                        failure_kind="upstream_contract",
                        file_path=f"{name}/{rel_path}",
                        http_status=int(response.status_code),
                        response_excerpt=response.text[:1000],
                    ) from exc
                if not isinstance(body, dict) or not body.get("success"):
                    raise RolloutDeliveryError(
                        f"/skillopt/update rejected {name}/{rel_path}: {body}",
                        retryable=False,
                        failure_kind="upstream_contract",
                        file_path=f"{name}/{rel_path}",
                        http_status=int(response.status_code),
                        response_excerpt=str(body)[:1000],
                    )
                files_written += 1
                if body.get("filePath"):
                    file_paths.append(str(body["filePath"]))
    except Exception as exc:  # noqa: BLE001 - delivery failures must be retried
        attempt += 1
        retryable = not isinstance(exc, RolloutDeliveryError) or exc.retryable
        status = (
            "dead_letter"
            if not retryable or attempt >= MAX_ATTEMPTS
            else "failed"
        )
        delay = min(3600.0, (2 ** attempt) + random.uniform(0.0, 1.0))
        delivery = {
            "status": "pending" if status == "failed" else "dead_letter",
            "workspace": workspace,
            "attempt": attempt,
            "next_retry_at": (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat(),
            "last_error": f"{type(exc).__name__}: {exc}"[:2000],
            "retryable": retryable,
            "failure_kind": getattr(exc, "failure_kind", "unexpected"),
        }
        for key in ("file_path", "http_status", "response_excerpt"):
            value = getattr(exc, key, None)
            if value not in (None, ""):
                delivery[key] = value
        service.record_delivery(event_id, CONSUMER_ID, delivery)
        return {
            "status": status,
            "workspace": workspace,
            "error": f"{type(exc).__name__}: {exc}",
            "attempt": attempt,
            "attempted": True,
            "retryable": retryable,
            "failure_kind": delivery["failure_kind"],
        }

    service.record_delivery(event_id, CONSUMER_ID, {
        "status": "synced",
        "workspace": workspace,
        "files_written": files_written,
        "file_paths": file_paths,
        "acked_at": datetime.now(timezone.utc).isoformat(),
        "last_error": "",
    })
    return {
        "status": "synced",
        "workspace": workspace,
        "files_written": files_written,
        "file_paths": file_paths,
        "attempted": True,
    }


async def rollout_tick(
    config: Any,
    service: Any,
    *,
    max_events: int = 10,
) -> dict[str, int]:
    """One consumer pass over the outbox; caps attempted events per tick."""
    synced = failed = 0
    attempted = 0
    for _, event in service.iter_outbox_events():
        if attempted >= max_events:
            break
        result = await deliver_rollout(config, service, event)
        if not bool(result.get("attempted")):
            continue
        attempted += 1
        status = str(result.get("status") or "")
        if status == "synced":
            synced += 1
            logger.info(
                "[skillopt-rollout] event=%s workspace=%s files=%s skill(s)=%s",
                event.get("event_id"),
                result.get("workspace"),
                result.get("files_written"),
                ",".join(
                    str(skill.get("name") or "")
                    for skill in _event_skills(event)
                    if not bool(skill.get("deleted"))
                ),
            )
        elif status in {"failed", "dead_letter"}:
            failed += 1
            logger.warning(
                "[skillopt-rollout] event=%s workspace=%s status=%s error=%s",
                event.get("event_id"),
                result.get("workspace"),
                status,
                result.get("error"),
            )
    return {"synced": synced, "failed": failed, "attempted": attempted}


async def rollout_tick_with_lease(
    config: Any,
    service: Any,
    *,
    max_events: int = 10,
) -> dict[str, Any]:
    """Run one enabled tenant tick while holding its cross-replica lease."""
    bucket = service.hub._bucket
    acquire = getattr(bucket, "try_background_lock", None)
    release = getattr(bucket, "release_background_lock", None)
    lease_supported = callable(acquire)
    if not bool(getattr(config, "skillopt_rollout_enabled", False)):
        return {
            "state": "disabled",
            "lease_supported": lease_supported,
            "lease_acquired": False,
            "lease_held": False,
            "synced": 0,
            "failed": 0,
            "attempted": 0,
        }
    if not str(getattr(config, "skillopt_rollout_endpoint", "") or "").strip():
        return {
            "state": "missing_endpoint",
            "lease_supported": lease_supported,
            "lease_acquired": False,
            "lease_held": False,
            "synced": 0,
            "failed": 0,
            "attempted": 0,
        }

    locked = False
    if callable(acquire):
        locked = await asyncio.to_thread(acquire, "skillopt-rollout-v1")
        if not locked:
            return {
                "state": "standby",
                "lease_supported": True,
                "lease_acquired": False,
                "lease_held": False,
                "synced": 0,
                "failed": 0,
                "attempted": 0,
            }
    try:
        summary = await rollout_tick(config, service, max_events=max_events)
        return {
            **summary,
            "state": "leader" if locked else "active",
            "lease_supported": lease_supported,
            "lease_acquired": locked,
            "lease_held": False,
        }
    finally:
        if locked and callable(release):
            await asyncio.to_thread(release, "skillopt-rollout-v1")
