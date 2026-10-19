"""In-memory object store for tests and mock-mode engines.

This is intentionally *not* a user-facing sharing backend. The only supported
sharing backends are cloud OpenViking and local (self-hosted) OpenViking, both
served by :class:`~teamEvolver.storage.viking.OpenVikingObjectStore`. This store
exists only so unit tests and the evolve engine's ``mock`` mode have a fast,
dependency-free object-store double that honors the same contract
(``get_object`` / ``put_object`` / ``delete_object`` / ``iter_objects``).
"""

from __future__ import annotations

import io
import threading
from typing import Iterator

from .base import ObjectInfo, _BytesObject, read_bytes


class InMemoryObjectStore:
    """Object store backed by an in-process dict.

    Keys are POSIX-style relative paths, matching the OpenViking store's key
    contract. Access is guarded by a lock so concurrent engine workers can
    share one instance safely.
    """

    def __init__(self, root: str | None = None) -> None:
        # ``root`` is accepted for signature parity with the previous
        # filesystem store; it is only used as an isolation namespace so
        # separate mock buckets never collide.
        self._root = str(root or "")
        self._data: dict[str, bytes] = {}
        self._lock = threading.RLock()

    def get_object(self, key: str) -> _BytesObject:
        clean = str(key or "").lstrip("/")
        with self._lock:
            if clean not in self._data:
                raise FileNotFoundError(f"InMemoryObjectStore: key not found: {key}")
            return _BytesObject(self._data[clean], clean)

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        clean = str(key or "").lstrip("/")
        with self._lock:
            self._data[clean] = read_bytes(data)

    def delete_object(self, key: str) -> None:
        clean = str(key or "").lstrip("/")
        with self._lock:
            self._data.pop(clean, None)

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        clean_prefix = str(prefix or "").lstrip("/")
        with self._lock:
            keys = sorted(k for k in self._data if k.startswith(clean_prefix))
        return iter(ObjectInfo(key) for key in keys)


# Registry of shared in-memory buckets keyed by a ``memory://<name>`` endpoint.
# This lets several stores (SkillHub, ValidationStore, SessionStore) built from
# the same config endpoint share one bucket within a process — the way the old
# filesystem backend shared a directory. It is only reachable via a
# ``memory://`` endpoint, which the UI/CLI/defaults never emit, so it stays a
# test/mock-only facility rather than a user-facing backend.
_SHARED_MEMORY_BUCKETS: dict[str, "InMemoryObjectStore"] = {}


def shared_memory_bucket(endpoint: str) -> "InMemoryObjectStore":
    """Return the process-shared in-memory bucket for a ``memory://`` endpoint."""
    key = str(endpoint or "").strip()
    bucket = _SHARED_MEMORY_BUCKETS.get(key)
    if bucket is None:
        bucket = InMemoryObjectStore(key)
        _SHARED_MEMORY_BUCKETS[key] = bucket
    return bucket


def is_memory_endpoint(endpoint: str) -> bool:
    return str(endpoint or "").strip().lower().startswith("memory://")
