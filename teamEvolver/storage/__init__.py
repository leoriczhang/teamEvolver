"""Shared object storage backends.

The primary user-facing sharing backend is ``viking`` (OpenViking), which
serves both supported deployments — cloud OpenViking and local self-hosted
OpenViking — differing only by endpoint. ``local`` selects the built-in
filesystem store (:class:`~teamEvolver.storage.local.LocalObjectStore`), which
is also the automatic fallback when the configured OpenViking deployment is
unavailable (see ``build_object_store(allow_fallback=...)``).
``InMemoryObjectStore`` implements the same contract for unit tests and
mock-mode engines and is never selectable as a sharing backend.

Public surface (kept stable for teamEvolver integrations and tests):
``build_object_store``, ``ObjectInfo``, ``InMemoryObjectStore``,
``LocalObjectStore``, ``OpenVikingObjectStore``, ``normalize_backend``,
``peer_key_prefix``, ``is_not_found_error``, ``probe_viking_availability``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .base import (
    ObjectInfo,
    is_not_found_error,
    normalize_backend,
    peer_key_prefix,
)
from .local import LocalObjectStore
from .memory import (
    InMemoryObjectStore,
    is_memory_endpoint,
    shared_memory_bucket,
)
from .pg_store import PgObjectStore
from .snapshot import (
    OpenVikingSnapshotClient,
    SnapshotBlob,
    SnapshotConflictError,
    SnapshotError,
    SnapshotNotFoundError,
    SnapshotPartialRestoreError,
    SnapshotProtocolError,
    SnapshotUnavailableError,
)
from .viking import _VIKING_ROOT_PREFIX, OpenVikingObjectStore, probe_viking_availability

logger = logging.getLogger(__name__)

__all__ = [
    "ObjectInfo",
    "InMemoryObjectStore",
    "LocalObjectStore",
    "OpenVikingObjectStore",
    "PgObjectStore",
    "OpenVikingSnapshotClient",
    "SnapshotBlob",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotNotFoundError",
    "SnapshotPartialRestoreError",
    "SnapshotProtocolError",
    "SnapshotUnavailableError",
    "build_object_store",
    "normalize_backend",
    "peer_key_prefix",
    "is_not_found_error",
    "probe_viking_availability",
]


def _default_local_root() -> str:
    """Default root for the built-in local storage fallback."""
    return str(Path.home() / ".teamEvolver" / "local_store")


def _effective_fallback_root(
    fallback_root: str,
    *,
    endpoint: str,
    account: str,
    user: str,
    root_prefix: str,
    group_id: str,
    namespace: str,
) -> str:
    """Derive the local directory used when a viking store falls back.

    The directory is namespaced per OpenViking identity
    (``{root}/{root_prefix}-{hash8}``) so two configurations never share one
    fallback bucket while staying human-browsable on disk.
    """
    base = str(fallback_root or "").strip() or _default_local_root()
    identity = "|".join(
        [
            str(endpoint or ""),
            str(account or ""),
            str(user or ""),
            str(root_prefix or ""),
            str(group_id or ""),
            str(namespace or ""),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    prefix = str(root_prefix or "").strip("/") or _VIKING_ROOT_PREFIX
    return str(Path(base).expanduser() / f"{prefix}-{digest}")


def build_object_store(
    *,
    backend: str | None,
    endpoint: str = "",
    local_root: str = "",
    viking_account: str = "",
    viking_user: str = "",
    viking_agent: str = "",
    viking_api_key: str = "",
    viking_agent_id: str = "",
    viking_root_prefix: str = "",
    viking_group_id: str = "",
    viking_namespace: str = "resources",
    allow_fallback: bool = False,
    fallback_root: str = "",
    # PostgreSQL local-state backend (multi-tenancy plan §2.4).
    pg_dsn: str = "",
    pg_schema: str = "teamevolver",
    pg_pool_min: int = 2,
    pg_pool_max: int = 20,
    pg_command_timeout: float = 30.0,
    tenant_id: str = "default",
    # Default >5s: macOS mDNS resolution of multi-label *.local hostnames
    # takes ~5s before unicast DNS kicks in, so a cold connection needs more
    # than 3s just for DNS. A too-short probe would wrongly classify a healthy
    # OpenViking as unavailable and silently fall back to local storage.
    fallback_probe_timeout: float = 10.0,
):
    """Create the configured object storage backend.

    ``viking`` (OpenViking, cloud or local self-hosted — they differ only by
    ``endpoint``) is the primary backend; ``local`` selects the built-in
    filesystem store rooted at ``local_root`` (default
    ``~/.teamEvolver/local_store``); ``postgres`` selects the multi-tenant
    local-state store (schema ``teamevolver`` on the shared OpenViking PG
    instance; ``pg_dsn`` empty → derived from ``OV_PG_*`` env vars).

    When the viking backend is selected and ``allow_fallback`` is true, the
    endpoint is probed once (verdicts cached ~30s); if it is unavailable —
    connection error, timeout, or HTTP 5xx — the built-in local store is
    returned instead so ingest/evolution keep working through an outage.
    ``local_root`` doubles as an alias for ``fallback_root`` in that case. For
    an in-process test/mock double, construct
    :class:`~teamEvolver.storage.memory.InMemoryObjectStore` directly or use a
    ``memory://`` endpoint.
    """
    resolved = normalize_backend(backend, endpoint=endpoint, local_root=local_root)
    if resolved == "local":
        root = str(local_root or fallback_root or "").strip() or _default_local_root()
        return LocalObjectStore(root)
    if resolved == "postgres":
        return PgObjectStore(
            dsn=pg_dsn,
            schema=pg_schema,
            tenant_id=tenant_id,
            pool_min=pg_pool_min,
            pool_max=pg_pool_max,
            command_timeout=pg_command_timeout,
        )
    if resolved == "viking":
        if not endpoint:
            raise ValueError("OpenViking storage backend requires an endpoint.")
        # A ``memory://`` endpoint selects a process-shared in-memory bucket.
        # This is a test/mock facility (never emitted by the UI/CLI/defaults),
        # letting stores built from the same endpoint share one bucket.
        if is_memory_endpoint(endpoint):
            return shared_memory_bucket(endpoint)
        store = OpenVikingObjectStore(
            endpoint=endpoint,
            api_key=viking_api_key,
            account=viking_account or "default",
            user=viking_user or "default",
            agent=viking_agent or _VIKING_ROOT_PREFIX,
            agent_id=viking_agent_id or "",
            root_prefix=viking_root_prefix or _VIKING_ROOT_PREFIX,
            group_id=viking_group_id or "",
            namespace=viking_namespace or "resources",
        )
        if allow_fallback:
            available, reason = probe_viking_availability(
                store, timeout=fallback_probe_timeout
            )
            if not available:
                root = _effective_fallback_root(
                    fallback_root or local_root,
                    endpoint=endpoint,
                    account=viking_account or "default",
                    user=viking_user or "default",
                    root_prefix=viking_root_prefix or _VIKING_ROOT_PREFIX,
                    group_id=viking_group_id or "",
                    namespace=viking_namespace or "resources",
                )
                logger.warning(
                    "OpenViking %s unavailable (%s) — falling back to built-in "
                    "local storage at %s",
                    endpoint,
                    reason,
                    root,
                )
                fallback_store = LocalObjectStore(root)
                # Stash provenance so status endpoints can explain the fallback
                # without re-probing the dead endpoint.
                fallback_store.fallback_active = True
                fallback_store.fallback_reason = reason
                fallback_store.fallback_endpoint = endpoint
                return fallback_store
        return store
    raise ValueError(f"Unsupported storage backend: {backend!r}")
