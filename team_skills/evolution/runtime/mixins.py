"""Shared helpers for evolve-server engine implementations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any

from teamEvolver.storage import InMemoryObjectStore, LocalObjectStore, build_object_store

from team_skills.evolution.kernel.enums import SLUG_RE
from team_skills.evolution.kernel.settings import EvolveServerConfig
from team_skills.evolution.store.object_store import append_history_record, list_session_keys, load_manifest, read_json_object

logger = logging.getLogger(__name__)


class EvolveEngineMixin:
    """Common storage, history, and naming behavior for evolve engines."""

    config: EvolveServerConfig
    _mock: bool
    _bucket: Any
    _prefix: str
    _session_prefix: str

    @staticmethod
    def _build_bucket(
        config: EvolveServerConfig,
        *,
        mock: bool = False,
        mock_root: str | None = None,
    ) -> Any:
        """Create the object-store adapter for an engine."""
        if mock:
            if not mock_root:
                raise ValueError("mock mode requires mock_root")
            return InMemoryObjectStore(mock_root)
        backend_normalized = str(config.storage_backend or "").strip().lower()
        if backend_normalized == "viking":
            return build_object_store(
                backend="viking",
                endpoint=getattr(config, "viking_endpoint", "") or config.storage_endpoint,
                viking_account=getattr(config, "viking_account", "") or "default",
                viking_user=getattr(config, "viking_user", "") or "team",
                viking_agent=getattr(config, "viking_agent", "") or "team-skill-evolver",
                viking_api_key=getattr(config, "viking_api_key", "") or "",
                viking_agent_id=getattr(config, "viking_agent_id", "") or "",
                viking_root_prefix=getattr(config, "viking_root_prefix", "") or "team-skill-evolver",
                viking_group_id=getattr(config, "viking_group_id", "") or "",
                allow_fallback=bool(getattr(config, "storage_fallback_enabled", True)),
                fallback_root=str(getattr(config, "storage_local_root", "") or ""),
            )
        if backend_normalized == "postgres":
            return build_object_store(
                backend="postgres",
                pg_dsn=str(getattr(config, "pg_dsn", "") or ""),
                pg_schema=str(getattr(config, "pg_schema", "") or "teamevolver"),
                pg_pool_min=max(1, int(getattr(config, "pg_pool_min", 2) or 2)),
                pg_pool_max=max(2, int(getattr(config, "pg_pool_max", 20) or 20)),
                pg_command_timeout=max(
                    1.0, float(getattr(config, "pg_command_timeout_seconds", 30.0) or 30.0)
                ),
                tenant_id=str(getattr(config, "pg_tenant_id", "default") or "default"),
            )
        return build_object_store(
            backend=config.storage_backend,
            endpoint=config.storage_endpoint,
            local_root=str(getattr(config, "storage_local_root", "") or ""),
            allow_fallback=bool(getattr(config, "storage_fallback_enabled", True)),
            fallback_root=str(getattr(config, "storage_local_root", "") or ""),
        )

    def _uses_local_storage(self) -> bool:
        """Return True when object-store calls are in-process and need no thread hop.

        Mock mode and the built-in local fallback store are in-process; the
        viking backend (cloud or local self-hosted OpenViking) is always remote
        HTTP and must run on the worker thread.
        """
        if self._mock:
            return True
        return isinstance(self._bucket, (InMemoryObjectStore, LocalObjectStore))

    async def _call_storage(self, func, *args):
        """Call storage helpers inline for local stores, in a worker for remote stores."""
        if self._uses_local_storage():
            return func(*args)
        return await asyncio.to_thread(func, *args)

    def _append_history(self, record: dict) -> None:
        """Append a JSONL history record without failing the engine cycle.

        Writes through the object store (bucket) so PG RLS / per-tenant
        Viking credentials enforce isolation.  The file-based
        ``evolve_history.jsonl`` is kept as a fallback for single-tenant /
        local-backend deployments and for backward-compatible CLI tools.
        """
        # Primary: write through the bucket (per-tenant isolated).
        try:
            append_history_record(self._bucket, record)
        except Exception as exc:  # noqa: BLE001
            if self.config.storage_backend == "postgres":
                raise
            logger.warning("[%s] history bucket write failed: %s", type(self).__name__, exc)
        # Fallback: also append to the local file (backward compat for CLI
        # tools that still read evolve_history.jsonl, and for local-backend
        # deployments where the bucket IS the local filesystem).
        if self.config.storage_backend == "postgres":
            return
        path = self.config.history_path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[%s] history file write failed: %s", type(self).__name__, exc)

    async def _drain_sessions(self) -> tuple[list[dict], list[str]]:
        """Read all queued session JSON objects and return payloads plus consumed keys.

        Objects are read in configurable batches with a small delay between
        batches so a large backlog does not saturate the storage backend (see
        ``evolve_drain_batch_size`` / ``evolve_drain_batch_delay_seconds``).
        """
        keys = await self._call_storage(list_session_keys, self._bucket, self._session_prefix)
        cap = int(
            getattr(self.config, "drain_max_per_cycle", 0)
            or getattr(self.config, "evolve_drain_max_per_cycle", 0)
            or 0
        )
        if cap > 0 and len(keys) > cap:
            logger.info(
                "[%s] capping drain to %d of %d queued session(s)",
                type(self).__name__,
                cap,
                len(keys),
            )
            keys = keys[:cap]
        batch_size = max(1, int(getattr(self.config, "evolve_drain_batch_size", 50) or 50))
        delay = max(0.0, float(getattr(self.config, "evolve_drain_batch_delay_seconds", 0.2) or 0.0))
        sessions: list[dict] = []
        consumed_keys: list[str] = []
        for start in range(0, len(keys), batch_size):
            if start:
                await asyncio.sleep(delay)
            for key in keys[start : start + batch_size]:
                if hasattr(self._bucket, "consume_session"):
                    raw = await self._call_storage(lambda k=key: self._bucket.get_object(k).read())
                    session = json.loads(raw)
                    session["_queue_sha256"] = hashlib.sha256(raw).hexdigest()
                else:
                    session = await self._call_storage(read_json_object, self._bucket, key)
                if session:
                    sessions.append(session)
                    consumed_keys.append(key)
        logger.info(
            "[%s] drained %d session(s) (%d keys found)",
            type(self).__name__,
            len(sessions),
            len(keys),
        )
        return sessions, consumed_keys

    def _load_remote_skills(self) -> dict[str, dict[str, Any]]:
        """Load the shared skill manifest for this engine's group prefix."""
        return load_manifest(self._bucket, self._prefix)

    @staticmethod
    def _sanitise_name(raw_name: str) -> str:
        """Normalize an arbitrary skill name into the storage slug format."""
        name = raw_name.strip().lower()
        if SLUG_RE.match(name):
            return name
        name = re.sub(r"[^a-z0-9_-]", "-", name).strip("-")
        return name or "unnamed-skill"
