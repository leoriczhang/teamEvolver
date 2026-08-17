"""Transactional team-Skill mutations with a durable sync outbox."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..integrations.skill_sync_adapters import sync_skill_event
from .hub import SkillHub


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _due(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class SkillMutationCommand:
    action: str
    name: str
    mutation_id: str
    skills_dir: str = ""
    target_version: int = 0
    tenant_ids: tuple[str, ...] = ()
    skill_filter: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillMutationService:
    """Deep module owning commit records, tombstones, outbox and delivery."""

    def __init__(
        self,
        *,
        hub: SkillHub,
        config: Any = None,
        deliverer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] = sync_skill_event,
    ) -> None:
        self._hub = hub
        self._bucket = hub._bucket
        self._config = config
        self._deliverer = deliverer

    @classmethod
    def from_config(cls, config: Any) -> "SkillMutationService":
        return cls(hub=SkillHub.team_from_config(config), config=config)

    @classmethod
    def from_hub(
        cls,
        hub: SkillHub,
        *,
        config: Any = None,
        deliverer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] = sync_skill_event,
    ) -> "SkillMutationService":
        return cls(hub=hub, config=config, deliverer=deliverer)

    @staticmethod
    def _commit_key(mutation_id: str) -> str:
        return f"skill_mutation_commits/{mutation_id}.json"

    @staticmethod
    def _event_key(event_id: str) -> str:
        return f"skill_sync_outbox/{event_id}.json"

    @staticmethod
    def _tombstone_key(name: str, version: int) -> str:
        return f"skill_tombstones/{name}/v{version}.json"

    def _read_json(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._bucket.get_object(key).read().decode("utf-8"))
        except Exception:
            return None

    def _write_json(self, key: str, value: dict[str, Any]) -> None:
        ensure_parent = getattr(self._bucket, "ensure_parent", None)
        if callable(ensure_parent):
            ensure_parent(key)
        self._bucket.put_object(
            key,
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    def _current(self, name: str) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in self._hub.list_remote()
                if str(item.get("name") or "") == name
            ),
            None,
        )

    def execute(self, command: SkillMutationCommand) -> dict[str, Any]:
        existing = self._read_json(self._commit_key(command.mutation_id))
        if existing:
            return dict(existing)
        action = str(command.action or "").strip().lower()
        name = str(command.name or "").strip()
        if action not in {"publish", "update", "rollback", "delete"} or not name:
            raise ValueError("action and skill name are required")
        before = self._current(name) or {}
        if action in {"publish", "update"}:
            if not command.skills_dir:
                raise ValueError("skills_dir is required")
            result = self._hub.push_skills(
                command.skills_dir,
                skill_filter=command.skill_filter,
                include_names=[name],
            )
            current = self._current(name)
            if current is None:
                raise RuntimeError("SkillHub commit did not produce a manifest record")
            result = {**result, "record": current}
            if int(result.get("uploaded") or 0) == 0:
                return {
                    "schema_version": "teamevolver.skill-mutation-noop.v1",
                    "mutation_id": command.mutation_id,
                    "action": action,
                    "expected": current,
                    "tenant_ids": sorted(set(command.tenant_ids)),
                    "event_id": "",
                    "result": result,
                    "metadata": dict(command.metadata),
                    "status": "unchanged",
                }
        elif action == "rollback":
            result = self._hub.rollback_skill(name, command.target_version)
            result = {key: value for key, value in result.items() if key != "bundle"}
            current = self._current(name)
            if current is None:
                raise RuntimeError("rollback did not produce a manifest record")
        else:
            tombstone_version = int(before.get("version") or 0) + 1
            result = self._hub.delete_skill(name)
            current = {
                "name": name,
                "version": tombstone_version,
                "sha256": str(before.get("sha256") or ""),
                "tree_sha256": str(before.get("tree_sha256") or ""),
                "deleted": True,
            }
            self._write_json(
                self._tombstone_key(name, tombstone_version),
                {**current, "deleted_at": _now(), "mutation_id": command.mutation_id},
            )
        return self.record_committed(
            action=action,
            mutation_id=command.mutation_id,
            expected=current,
            tenant_ids=list(command.tenant_ids),
            result=result,
            metadata=command.metadata,
        )

    def record_committed(
        self,
        *,
        action: str,
        mutation_id: str,
        expected: dict[str, Any],
        tenant_ids: list[str],
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected_fingerprint = _stable_id("expected", expected)
        event_id = ""
        for item in self._bucket.iter_objects("skill_sync_outbox/"):
            candidate = self._read_json(item.key)
            skills = candidate.get("skills") if candidate else []
            if (
                isinstance(skills, list)
                and skills
                and isinstance(skills[0], dict)
                and _stable_id("expected", skills[0]) == expected_fingerprint
            ):
                event_id = str(candidate.get("event_id") or "")
                break
        if not event_id:
            event_id = _stable_id(
                "skill_evt",
                {
                    "action": action,
                    "expected": expected,
                },
            )
        existing = self._read_json(self._commit_key(mutation_id)) or {}
        merged_tenants = sorted(
            {
                *[
                    str(item)
                    for item in existing.get("tenant_ids") or []
                    if str(item)
                ],
                *[str(item) for item in tenant_ids if str(item)],
            }
        )
        commit = {
            "schema_version": "teamevolver.skill-mutation-commit.v1",
            "mutation_id": mutation_id,
            "action": action,
            "expected": dict(expected),
            "tenant_ids": merged_tenants,
            "event_id": event_id,
            "result": {**dict(existing.get("result") or {}), **dict(result or {})},
            "metadata": {
                **dict(existing.get("metadata") or {}),
                **dict(metadata or {}),
            },
            "committed_at": str(existing.get("committed_at") or _now()),
        }
        self._write_json(self._commit_key(mutation_id), commit)
        event = self._read_json(self._event_key(event_id))
        if event is None:
            self._write_json(
                self._event_key(event_id),
                {
                    "schema_version": "teamevolver.skill-sync-outbox.v1",
                    "event_id": event_id,
                    "action": action,
                    "mutation_id": mutation_id,
                    "skills": [dict(expected)],
                    "tenant_ids": commit["tenant_ids"],
                    "status": "pending",
                    "attempt": 0,
                    "next_retry_at": _now(),
                    "deliveries": {},
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            )
        elif merged_tenants != list(event.get("tenant_ids") or []):
            event["tenant_ids"] = merged_tenants
            event["updated_at"] = _now()
            self._write_json(self._event_key(event_id), event)
        return commit

    def reconcile(self) -> int:
        repaired = 0
        committed_fingerprints: set[str] = set()
        for item in self._bucket.iter_objects("skill_mutation_commits/"):
            commit = self._read_json(item.key)
            if not commit:
                continue
            committed_fingerprints.add(
                _stable_id(
                    "expected",
                    dict(commit.get("expected") or {}),
                )
            )
            event_id = str(commit.get("event_id") or "")
            if event_id and self._read_json(self._event_key(event_id)) is None:
                self.record_committed(
                    action=str(commit.get("action") or ""),
                    mutation_id=str(commit.get("mutation_id") or ""),
                    expected=dict(commit.get("expected") or {}),
                    tenant_ids=list(commit.get("tenant_ids") or []),
                    result=dict(commit.get("result") or {}),
                    metadata=dict(commit.get("metadata") or {}),
                )
                repaired += 1
        for item in self._bucket.iter_objects("skill_tombstones/"):
            tombstone = self._read_json(item.key)
            if not tombstone:
                continue
            expected = {
                key: tombstone.get(key)
                for key in (
                    "name",
                    "version",
                    "sha256",
                    "tree_sha256",
                    "deleted",
                )
            }
            fingerprint = _stable_id("expected", expected)
            if fingerprint in committed_fingerprints:
                continue
            mutation_id = str(tombstone.get("mutation_id") or "")
            if not mutation_id:
                continue
            self.record_committed(
                action="delete",
                mutation_id=mutation_id,
                expected=expected,
                tenant_ids=[],
                metadata={"reconciled_from": item.key},
            )
            committed_fingerprints.add(fingerprint)
            repaired += 1
        for current in self._hub.list_remote():
            expected = dict(current)
            fingerprint = _stable_id("expected", expected)
            if fingerprint in committed_fingerprints:
                continue
            name = str(expected.get("name") or "")
            version = int(expected.get("version") or 0)
            if not name or version <= 0:
                continue
            self.record_committed(
                action="update",
                mutation_id=_stable_id(
                    "reconcile",
                    {
                        "name": name,
                        "version": version,
                        "sha256": expected.get("sha256"),
                        "tree_sha256": expected.get("tree_sha256"),
                    },
                ),
                expected=expected,
                tenant_ids=[],
                metadata={"reconciled_from": "manifest.json"},
            )
            committed_fingerprints.add(fingerprint)
            repaired += 1
        return repaired

    async def drain(self, *, limit: int = 100) -> dict[str, int]:
        summary = {"synced": 0, "failed": 0, "pending": 0}
        for item in list(self._bucket.iter_objects("skill_sync_outbox/"))[:limit]:
            event = self._read_json(item.key)
            if not event or event.get("status") in {"synced", "cancelled"}:
                continue
            if not _due(event.get("next_retry_at")):
                summary["pending"] += 1
                continue
            try:
                result = await self._deliverer(self._config, event)
                deliveries = dict(event.get("deliveries") or {})
                for integration_id, delivery_result in dict(
                    result.get("results") or {}
                ).items():
                    current = dict(deliveries.get(integration_id) or {})
                    status = str(delivery_result.get("status") or "failed")
                    if status == "synced":
                        deliveries[integration_id] = {
                            **current,
                            **dict(delivery_result),
                            "status": "synced",
                            "acked_at": _now(),
                            "last_error": "",
                        }
                        continue
                    if status == "cancelled":
                        deliveries[integration_id] = {
                            **current,
                            **dict(delivery_result),
                            "status": "cancelled",
                            "cancelled_at": _now(),
                        }
                        continue
                    if not bool(delivery_result.get("attempted", True)):
                        deliveries[integration_id] = {
                            **current,
                            **dict(delivery_result),
                            "status": current.get("status") or "pending",
                        }
                        continue
                    attempt = int(current.get("attempt") or 0) + 1
                    delay = min(3600, 2 ** attempt)
                    deliveries[integration_id] = {
                        **current,
                        **dict(delivery_result),
                        "attempt": attempt,
                        "status": (
                            "dead_letter" if attempt >= 8 else "pending"
                        ),
                        "next_retry_at": (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=delay)
                        ).isoformat(),
                        "last_error": str(
                            delivery_result.get("detail")
                            or delivery_result.get("reason")
                            or "Skill sync failed"
                        )[:2000],
                    }
                event["deliveries"] = deliveries
                terminal = {
                    str(value.get("status") or "")
                    for value in deliveries.values()
                    if isinstance(value, dict)
                }
                event["attempt"] = max(
                    [
                        int(value.get("attempt") or 0)
                        for value in deliveries.values()
                        if isinstance(value, dict)
                    ]
                    or [int(event.get("attempt") or 0)]
                )
                if str(result.get("status") or "") == "no_capable_agents":
                    terminal.add("cancelled")
                if terminal and terminal.issubset({"synced", "cancelled"}):
                    event["status"] = "synced"
                    event["acked_at"] = _now()
                    summary["synced"] += 1
                elif "dead_letter" in terminal and not (
                    terminal & {"pending", "failed"}
                ):
                    event["status"] = "dead_letter"
                    summary["failed"] += 1
                else:
                    event["status"] = "pending"
                    due_values = [
                        str(value.get("next_retry_at") or "")
                        for value in deliveries.values()
                        if isinstance(value, dict)
                        and value.get("status") == "pending"
                    ]
                    event["next_retry_at"] = min(due_values or [_now()])
                    summary["failed"] += 1
            except Exception as exc:
                event["attempt"] = int(event.get("attempt") or 0) + 1
                event["last_error"] = f"{type(exc).__name__}: {exc}"[:2000]
                if event["attempt"] >= 8:
                    event["status"] = "dead_letter"
                else:
                    event["status"] = "pending"
                    delay = min(3600, 2 ** event["attempt"])
                    event["next_retry_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                summary["failed"] += 1
            event["updated_at"] = _now()
            self._write_json(item.key, event)
        return summary

    def health(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        backlog = 0
        dead_letter = 0
        oldest_seconds = 0
        last_error = ""
        for item in self._bucket.iter_objects("skill_sync_outbox/"):
            event = self._read_json(item.key)
            if not event:
                continue
            status = str(event.get("status") or "pending")
            if status in {"synced", "cancelled"}:
                continue
            backlog += 1
            dead_letter += int(status == "dead_letter")
            try:
                created = datetime.fromisoformat(str(event.get("created_at") or ""))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                oldest_seconds = max(
                    oldest_seconds,
                    max(0, int((now - created).total_seconds())),
                )
            except (TypeError, ValueError):
                pass
            if event.get("last_error"):
                last_error = str(event["last_error"])
            for delivery in (event.get("deliveries") or {}).values():
                if isinstance(delivery, dict) and delivery.get("last_error"):
                    last_error = str(delivery["last_error"])
        return {
            "backlog": backlog,
            "oldest_age_seconds": oldest_seconds,
            "dead_letter": dead_letter,
            "last_error": last_error,
        }

    def retry(
        self,
        event_id: str,
        *,
        integration_id: str = "",
    ) -> dict[str, Any]:
        event = self._read_json(self._event_key(event_id))
        if not event:
            raise KeyError(event_id)
        deliveries = dict(event.get("deliveries") or {})
        targets = [integration_id] if integration_id else list(deliveries)
        for target in targets:
            delivery = dict(deliveries.get(target) or {})
            delivery.update(
                {
                    "status": "pending",
                    "attempt": 0,
                    "next_retry_at": _now(),
                    "last_error": "",
                }
            )
            deliveries[target] = delivery
        event["deliveries"] = deliveries
        event["status"] = "pending"
        event["attempt"] = 0
        event["next_retry_at"] = _now()
        event["updated_at"] = _now()
        self._write_json(self._event_key(event_id), event)
        return event

    def discard(
        self,
        event_id: str,
        *,
        integration_id: str = "",
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        event = self._read_json(self._event_key(event_id))
        if not event:
            raise KeyError(event_id)
        deliveries = dict(event.get("deliveries") or {})
        targets = [integration_id] if integration_id else list(deliveries)
        for target in targets:
            delivery = dict(deliveries.get(target) or {})
            delivery.update(
                {
                    "status": "cancelled",
                    "cancelled_at": _now(),
                    "cancelled_by": actor,
                    "cancel_reason": reason,
                }
            )
            deliveries[target] = delivery
        event["deliveries"] = deliveries
        event["status"] = (
            "cancelled"
            if not deliveries
            or all(
                value.get("status") in {"synced", "cancelled"}
                for value in deliveries.values()
                if isinstance(value, dict)
            )
            else "pending"
        )
        event.setdefault("audit", []).append(
            {
                "action": "discard",
                "integration_id": integration_id,
                "actor": actor,
                "reason": reason,
                "at": _now(),
            }
        )
        event["updated_at"] = _now()
        self._write_json(self._event_key(event_id), event)
        return event
