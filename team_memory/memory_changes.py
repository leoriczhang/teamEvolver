"""OpenViking-backed ledger for historical and ad-hoc Memory replay."""

from __future__ import annotations

import json
from typing import Any, Mapping

from team_memory.openviking import parse_openviking_key
from teamEvolver.storage import (
    InMemoryObjectStore,
    LocalObjectStore,
    OpenVikingSnapshotClient,
    SnapshotNotFoundError,
    build_object_store,
)
from teamEvolver.storage.memory import is_memory_endpoint

_LEDGER_PREFIX = "memory-changes"
_REPLAY_PREFIX = "memory-replays"


class MemoryChangeLedger:
    """Read historical Memory changes and persist Replay results."""

    def __init__(
        self,
        *,
        snapshot_client: OpenVikingSnapshotClient | None,
        object_store: Any,
    ) -> None:
        self._snapshot = snapshot_client
        self._store = object_store

    @classmethod
    def from_config(cls, config: Any) -> "MemoryChangeLedger":
        """Build a historical-change reader from the current app config."""
        endpoint = str(getattr(config, "sharing_viking_endpoint", "") or "")
        api_key = str(getattr(config, "sharing_viking_api_key", "") or "")
        key_account, key_user = parse_openviking_key(api_key)
        account = str(
            key_account
            or getattr(config, "sharing_viking_account", "")
            or "default"
        )
        owner_user = str(
            key_user
            or getattr(config, "sharing_viking_user", "")
            or "team"
        )
        agent = str(
            getattr(config, "sharing_viking_agent", "")
            or "team-skill-evolver"
        )
        store = (
            build_object_store(
                backend="viking",
                endpoint=endpoint,
                viking_account=account,
                viking_user=owner_user,
                viking_agent=agent,
                viking_api_key=api_key,
                viking_root_prefix="team-skill-evolver",
                allow_fallback=True,
            )
            if endpoint
            else InMemoryObjectStore("unconfigured-team-memory-ledger")
        )
        snapshot = (
            None
            # When the object store fell back to the built-in local backend,
            # the OpenViking endpoint is down — the snapshot client would only
            # hammer a dead server, so snapshot capture is disabled too.
            if not endpoint or is_memory_endpoint(endpoint) or isinstance(store, LocalObjectStore)
            else OpenVikingSnapshotClient(
                endpoint=endpoint,
                api_key=api_key,
                account=account,
                user=owner_user,
                agent=agent,
            )
        )
        return cls(
            snapshot_client=snapshot,
            object_store=store,
        )

    def list_changes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in self._store.iter_objects(f"{_LEDGER_PREFIX}/"):
            key = str(item.key)
            if not key.endswith(".json"):
                continue
            try:
                value = json.loads(
                    self._store.get_object(key).read().decode("utf-8")
                )
            except (FileNotFoundError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        records.sort(
            key=lambda item: str(item.get("completed_at") or ""),
            reverse=True,
        )
        return records[: max(1, int(limit))]

    def load_change(self, change_id: str) -> dict[str, Any] | None:
        wanted = str(change_id or "").strip()
        if not wanted:
            return None
        for record in self.list_changes(limit=10000):
            if str(record.get("change_id") or "") == wanted:
                return record
        return None

    def read_snapshot_text(self, *, oid: str, path: str) -> str:
        if self._snapshot is None:
            raise RuntimeError("OpenViking Snapshot is unavailable")
        try:
            blob = self._snapshot.show_blob(oid, path=path, raw=False)
        except SnapshotNotFoundError:
            return ""
        return blob.content.decode("utf-8")

    def save_replay(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        replay = dict(payload)
        change_id = str(replay.get("change_id") or "").strip()
        replay_id = str(replay.get("replay_id") or "").strip()
        if not change_id or not replay_id:
            raise ValueError("memory replay requires change_id and replay_id")
        key = f"{_REPLAY_PREFIX}/{change_id}/{replay_id}.json"
        replay["record_key"] = key
        replay["ledger_status"] = "persisted"
        self._persist(key, replay)
        return replay

    def list_replays(
        self,
        *,
        change_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        wanted = str(change_id or "").strip()
        prefix = (
            f"{_REPLAY_PREFIX}/{wanted}/"
            if wanted
            else f"{_REPLAY_PREFIX}/"
        )
        records: list[dict[str, Any]] = []
        for item in self._store.iter_objects(prefix):
            key = str(item.key)
            if not key.endswith(".json"):
                continue
            try:
                value = json.loads(
                    self._store.get_object(key).read().decode("utf-8")
                )
            except (FileNotFoundError, UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        records.sort(
            key=lambda item: str(item.get("completed_at") or ""),
            reverse=True,
        )
        return records[: max(1, int(limit))]

    def close(self) -> None:
        if self._snapshot is not None:
            self._snapshot.close()

    def _persist(self, key: str, record: Mapping[str, Any]) -> None:
        body = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        batch_write = getattr(self._store, "batch_write", None)
        if callable(batch_write):
            batch_write(
                {key: body},
                preconditions={key: {"kind": "create_if_absent"}},
            )
            return
        self._store.put_object(key, body)
