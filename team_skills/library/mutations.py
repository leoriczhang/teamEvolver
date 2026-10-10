"""Transactional team-Skill mutations with optional Skillopt rollout."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from team_skills.library.hub import SkillHub


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    target_version: int = 0
    tenant_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillMutationService:
    """Own commit records, tombstones, and optional rollout events."""

    def __init__(
        self,
        *,
        hub: SkillHub,
        config: Any = None,
    ) -> None:
        self._hub = hub
        self._bucket = hub._bucket
        self._prefix = hub._prefix()
        self._config = config
        # Commit and tombstone history is append-only, so reconciliation only
        # needs to inspect each object once per process.
        self._reconciled_keys: set[str] = set()
        self._committed_fingerprints: set[str] = set()

    @property
    def hub(self) -> SkillHub:
        return self._hub

    @classmethod
    def from_config(cls, config: Any, tenant_id: str = "default") -> "SkillMutationService":
        return cls(hub=SkillHub.team_from_config(config, tenant_id=tenant_id), config=config)

    @classmethod
    def from_hub(
        cls,
        hub: SkillHub,
        *,
        config: Any = None,
    ) -> "SkillMutationService":
        return cls(hub=hub, config=config)

    def _commit_key(self, mutation_id: str) -> str:
        return f"{self._prefix}skill_mutation_commits/{mutation_id}.json"

    def _event_key(self, event_id: str) -> str:
        return f"{self._prefix}skill_rollout_outbox/{event_id}.json"

    def _tombstone_key(self, name: str, version: int) -> str:
        return f"{self._prefix}skill_tombstones/{name}/v{version}.json"

    def _family_prefix(self, family: str) -> str:
        return f"{self._prefix}{family.strip('/')}/"

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
                for item in self._hub.list_skills()
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
        if action not in {"rollback", "delete"} or not name:
            raise ValueError("rollback/delete action and skill name are required")
        before = self._current(name) or {}
        if action == "rollback":
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

    def publish_bundle(
        self,
        *,
        action: str,
        name: str,
        mutation_id: str,
        bundle: dict[str, bytes],
        tenant_ids: list[str] | tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish an in-memory bundle and durably enqueue its delivery."""
        existing = self._read_json(self._commit_key(mutation_id))
        if existing:
            return dict(existing)
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"publish", "update"}:
            raise ValueError("bundle publication action must be publish or update")
        skill_name = str(name or "").strip()
        if not skill_name:
            raise ValueError("skill name is required")
        result = self._hub.publish_bundle(
            skill_name,
            bundle,
            action=normalized_action,
        )
        current = dict(result.get("record") or {})
        if not current:
            current = self._current(skill_name) or {}
        if not current:
            raise RuntimeError("SkillHub commit did not produce a manifest record")
        if int(result.get("uploaded") or 0) == 0:
            return {
                "schema_version": "teamevolver.skill-mutation-noop.v1",
                "mutation_id": mutation_id,
                "action": normalized_action,
                "expected": current,
                "tenant_ids": sorted(set(tenant_ids)),
                "event_id": "",
                "result": result,
                "metadata": dict(metadata or {}),
                "status": "unchanged",
            }
        return self.record_committed(
            action=normalized_action,
            mutation_id=mutation_id,
            expected=current,
            tenant_ids=list(tenant_ids),
            result=result,
            metadata=metadata,
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
        rollout_enabled = bool(
            getattr(self._config, "skillopt_rollout_enabled", False)
        )
        event_id = (
            str(existing.get("event_id") or "")
            if rollout_enabled
            else ""
        )
        if rollout_enabled and not event_id:
            event_id = _stable_id(
                "skill_rollout_evt",
                {"action": action, "expected": expected},
            )
        commit: dict[str, Any] = {
            "schema_version": "teamevolver.skill-mutation-commit.v1",
            "mutation_id": mutation_id,
            "action": action,
            "expected": dict(expected),
            "tenant_ids": merged_tenants,
            "event_id": event_id,
            "status": "published",
            "delivery_mode": "pull",
            "result": {**dict(existing.get("result") or {}), **dict(result or {})},
            "metadata": {
                **dict(existing.get("metadata") or {}),
                **dict(metadata or {}),
            },
            "committed_at": str(existing.get("committed_at") or _now()),
        }
        event = None
        if rollout_enabled:
            event = self._read_json(self._event_key(event_id))
            if event is None:
                event = {
                    "schema_version": "teamevolver.skill-rollout-outbox.v1",
                    "event_id": event_id,
                    "action": action,
                    "mutation_id": mutation_id,
                    "skills": [dict(expected)],
                    "tenant_ids": merged_tenants,
                    "deliveries": {},
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            elif merged_tenants != list(event.get("tenant_ids") or []):
                event["tenant_ids"] = merged_tenants
                event["updated_at"] = _now()
        if event is not None and bool(getattr(self._bucket, "native_batch_write", False)):
            commit_key = self._commit_key(mutation_id)
            event_key = self._event_key(event_id)
            for attempt in range(3):
                preconditions = {
                    commit_key: self._bucket.object_precondition(commit_key),
                    event_key: self._bucket.object_precondition(event_key),
                }
                try:
                    self._bucket.batch_write(
                        {
                            commit_key: json.dumps(
                                commit,
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8"),
                            event_key: json.dumps(
                                event,
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8"),
                        },
                        preconditions=preconditions,
                    )
                    break
                except RuntimeError:
                    persisted = self._read_json(commit_key)
                    if persisted:
                        return persisted
                    if attempt == 2:
                        raise
                    event = self._read_json(event_key) or event
                    event["tenant_ids"] = sorted(
                        set(event.get("tenant_ids") or []) | set(merged_tenants)
                    )
                    event["updated_at"] = _now()
        else:
            self._write_json(self._commit_key(mutation_id), commit)
            if event is not None:
                self._write_json(self._event_key(event_id), event)
        return commit

    def iter_rollout_events(self):
        """Yield current rollout events."""
        for item in self._bucket.iter_objects(
            self._family_prefix("skill_rollout_outbox")
        ):
            event = self._read_json(item.key)
            if event and event.get("event_id"):
                yield item.key, event

    def record_delivery(self, event_id: str, consumer_id: str, delivery: dict[str, Any]) -> dict[str, Any]:
        """Merge one consumer's delivery record into its outbox event.

        Re-reads the event before writing so concurrent drain() updates for
        other consumers are preserved.
        """
        key = self._event_key(event_id)
        attempts = 3 if bool(getattr(self._bucket, "native_batch_write", False)) else 1
        for attempt in range(attempts):
            precondition = None
            if attempts > 1:
                try:
                    raw_event = self._bucket.get_object(key).read()
                    event = json.loads(raw_event.decode("utf-8"))
                    precondition = {
                        "kind": "replace_if_hash",
                        "base_hash": "sha256:" + hashlib.sha256(raw_event).hexdigest(),
                    }
                except Exception:
                    event = None
            else:
                event = self._read_json(key)
            if not event:
                raise KeyError(event_id)
            deliveries = dict(event.get("deliveries") or {})
            current = dict(deliveries.get(consumer_id) or {})
            current.update(dict(delivery))
            deliveries[consumer_id] = current
            event["deliveries"] = deliveries
            event["updated_at"] = _now()
            if attempts == 1:
                self._write_json(key, event)
            else:
                try:
                    self._bucket.batch_write(
                        {
                            key: json.dumps(
                                event,
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8")
                        },
                        preconditions={
                            key: dict(precondition or {})
                        },
                    )
                except RuntimeError:
                    if attempt + 1 >= attempts:
                        raise
                    continue
            return event
        raise RuntimeError(f"failed to update outbox event: {event_id}")

    def reconcile(self) -> int:
        repaired = 0
        for item in self._bucket.iter_objects(
            self._family_prefix("skill_mutation_commits")
        ):
            if item.key in self._reconciled_keys:
                continue
            commit = self._read_json(item.key)
            self._reconciled_keys.add(item.key)
            if not commit:
                continue
            self._committed_fingerprints.add(
                _stable_id(
                    "expected",
                    dict(commit.get("expected") or {}),
                )
            )
            event_id = str(commit.get("event_id") or "")
            rollout_enabled = bool(
                getattr(self._config, "skillopt_rollout_enabled", False)
            )
            event_missing = not event_id or (
                self._read_json(self._event_key(event_id)) is None
            )
            if rollout_enabled and event_missing:
                self.record_committed(
                    action=str(commit.get("action") or ""),
                    mutation_id=str(commit.get("mutation_id") or ""),
                    expected=dict(commit.get("expected") or {}),
                    tenant_ids=list(commit.get("tenant_ids") or []),
                    result=dict(commit.get("result") or {}),
                    metadata=dict(commit.get("metadata") or {}),
                )
                repaired += 1
        for item in self._bucket.iter_objects(
            self._family_prefix("skill_tombstones")
        ):
            if item.key in self._reconciled_keys:
                continue
            tombstone = self._read_json(item.key)
            self._reconciled_keys.add(item.key)
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
            if fingerprint in self._committed_fingerprints:
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
            self._committed_fingerprints.add(fingerprint)
            repaired += 1
        for current in self._hub.list_skills():
            expected = dict(current)
            fingerprint = _stable_id("expected", expected)
            if fingerprint in self._committed_fingerprints:
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
            self._committed_fingerprints.add(fingerprint)
            repaired += 1
        return repaired
