"""Shared object storage backends.

The only user-facing sharing backend is ``viking`` (OpenViking), which serves
both supported deployments — cloud OpenViking and local self-hosted
OpenViking — differing only by endpoint. ``InMemoryObjectStore`` implements the
same contract for unit tests and mock-mode engines and is never selectable as a
sharing backend.

Public surface (kept stable for teamEvolver integrations and tests):
``build_object_store``, ``ObjectInfo``, ``InMemoryObjectStore``,
``OpenVikingObjectStore``, ``normalize_backend``, ``peer_key_prefix``,
``is_not_found_error``.
"""

from __future__ import annotations

from .base import (
    ObjectInfo,
    is_not_found_error,
    normalize_backend,
    peer_key_prefix,
)
from .memory import (
    InMemoryObjectStore,
    is_memory_endpoint,
    shared_memory_bucket,
)
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
from .viking import _VIKING_ROOT_PREFIX, OpenVikingObjectStore

__all__ = [
    "ObjectInfo",
    "InMemoryObjectStore",
    "OpenVikingObjectStore",
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
]


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
):
    """Create the configured object storage backend.

    Only OpenViking (``viking``) is supported, covering both cloud and local
    self-hosted deployments (they differ only by ``endpoint``). ``local_root``
    is accepted for signature compatibility but ignored — the filesystem
    backend has been removed. For an in-process test/mock double, construct
    :class:`~teamEvolver.storage.memory.InMemoryObjectStore` directly.
    """
    resolved = normalize_backend(backend, endpoint=endpoint, local_root=local_root)
    if resolved == "viking":
        if not endpoint:
            raise ValueError("OpenViking storage backend requires an endpoint.")
        # A ``memory://`` endpoint selects a process-shared in-memory bucket.
        # This is a test/mock facility (never emitted by the UI/CLI/defaults),
        # letting stores built from the same endpoint share one bucket.
        if is_memory_endpoint(endpoint):
            return shared_memory_bucket(endpoint)
        return OpenVikingObjectStore(
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
    raise ValueError(f"Unsupported storage backend: {backend!r}")
