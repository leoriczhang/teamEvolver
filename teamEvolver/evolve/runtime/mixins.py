"""Shared helpers for evolve-server engine implementations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from ...storage import LocalObjectStore, build_object_store

from ..kernel.enums import SLUG_RE
from ..kernel.settings import EvolveServerConfig
from ..store.object_store import list_session_keys, load_manifest, read_json_object

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
            return LocalObjectStore(mock_root)
        backend_normalized = str(config.storage_backend or "").strip().lower()
        if backend_normalized == "viking":
            return build_object_store(
                backend="viking",
                endpoint=getattr(config, "viking_endpoint", "") or config.storage_endpoint,
                local_root="",
                viking_account=getattr(config, "viking_account", "") or "default",
                viking_user=getattr(config, "viking_user", "") or "default",
                viking_agent=getattr(config, "viking_agent", "") or "team-skill-evolver",
                viking_api_key=getattr(config, "viking_api_key", "") or "",
                viking_agent_id=getattr(config, "viking_agent_id", "") or "",
                viking_root_prefix=getattr(config, "viking_root_prefix", "") or "team-skill-evolver",
                viking_group_id=getattr(config, "viking_group_id", "") or "",
            )
        return build_object_store(
            backend=config.storage_backend,
            endpoint=config.storage_endpoint,
            local_root=config.local_root,
        )

    def _uses_local_storage(self) -> bool:
        """Return True when object-store calls are local and need no thread hop."""
        backend = str(self.config.storage_backend or "").strip().lower()
        if backend == "local" or self._mock:
            return True
        if backend == "viking":
            # OpenViking is always remote; force the worker thread path.
            return False
        bucket_type = type(self._bucket).__name__.lower()
        return "local" in bucket_type and bool(self.config.local_root)

    async def _call_storage(self, func, *args):
        """Call storage helpers inline for local stores, in a worker for remote stores."""
        if self._uses_local_storage():
            return func(*args)
        return await asyncio.to_thread(func, *args)

    def _append_history(self, record: dict) -> None:
        """Append a JSONL history record without failing the engine cycle."""
        path = self.config.history_path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[%s] history write failed: %s", type(self).__name__, exc)

    async def _drain_sessions(self) -> tuple[list[dict], list[str]]:
        """Read all queued session JSON objects and return payloads plus consumed keys."""
        keys = await self._call_storage(list_session_keys, self._bucket, self._session_prefix)
        sessions: list[dict] = []
        consumed_keys: list[str] = []
        for key in keys:
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
