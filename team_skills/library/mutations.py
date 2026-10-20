"""Transactional team-Skill mutations over OpenViking object storage."""

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
    """Own commit records and tombstones."""

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
        """Publish an in-memory bundle and record its commit."""
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
        commit: dict[str, Any] = {
            "schema_version": "teamevolver.skill-mutation-commit.v1",
            "mutation_id": mutation_id,
            "action": action,
            "expected": dict(expected),
            "tenant_ids": merged_tenants,
            "event_id": "",
            "status": "published",
            "delivery_mode": "pull",
            "result": {**dict(existing.get("result") or {}), **dict(result or {})},
            "metadata": {
                **dict(existing.get("metadata") or {}),
                **dict(metadata or {}),
            },
            "committed_at": str(existing.get("committed_at") or _now()),
        }
        if bool(getattr(self._bucket, "native_batch_write", False)):
            commit_key = self._commit_key(mutation_id)
            try:
                self._bucket.batch_write(
                    {
                        commit_key: json.dumps(
                            commit,
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8"),
                    },
                    preconditions={
                        commit_key: self._bucket.object_precondition(commit_key),
                    },
                )
            except RuntimeError:
                persisted = self._read_json(commit_key)
                if persisted:
                    return persisted
                raise
        else:
            self._write_json(self._commit_key(mutation_id), commit)
        return commit

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
