"""Shared object storage backends.

Public surface (kept stable for teamEvolver integrations and tests):
``build_object_store``, ``ObjectInfo``, ``LocalObjectStore``,
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
from .local import LocalObjectStore
from .viking import _VIKING_ROOT_PREFIX, OpenVikingObjectStore

__all__ = [
    "ObjectInfo",
    "LocalObjectStore",
    "OpenVikingObjectStore",
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
    """Create the configured object storage backend (``local`` or ``viking``)."""
    resolved = normalize_backend(backend, endpoint=endpoint, local_root=local_root)
    if resolved == "local":
        if not local_root:
            raise ValueError("Local storage backend requires local_root.")
        return LocalObjectStore(local_root)
    if resolved == "viking":
        if not endpoint:
            raise ValueError("OpenViking storage backend requires an endpoint.")
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
