"""Filesystem-backed object store — teamEvolver's built-in fallback backend.

This store implements the same object-store contract as
:class:`~teamEvolver.storage.viking.OpenVikingObjectStore`
(``get_object`` / ``put_object`` / ``delete_object`` / ``iter_objects``) on top
of a local directory. It is the *built-in storage* the system falls back to
when the configured OpenViking deployment (cloud Volcengine or a local /
self-hosted ``openviking-server``) is unavailable, so ingest / evolution /
validation keep working through an outage.

It intentionally does NOT define ``native_batch_write`` / ``batch_write``:
every batch caller already degrades to per-object writes when the attribute is
absent (see ``evolve/kernel/registry.py``, ``evolve/runtime/orchestrator.py``,
``validation/store.py``, ``dreamcycle/memory_changes.py``).

Data written here while OpenViking is down stays local — there is no automatic
sync-back when the remote returns.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path
from typing import Iterator

from .base import ObjectInfo, _BytesObject, read_bytes


class LocalObjectStore:
    """Filesystem-backed object store rooted at a local directory."""

    def __init__(self, root: str | Path) -> None:
        self._root_path = Path(root).expanduser()
        self._root = str(self._root_path)
        self._lock = threading.RLock()

    @property
    def root(self) -> str:
        """Absolute-ized root directory (created lazily on first write)."""
        return self._root

    def _resolve(self, key: str) -> Path:
        clean = str(key or "").replace("\\", "/").lstrip("/")
        if not clean:
            raise ValueError("LocalObjectStore: empty key")
        parts = [part for part in clean.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError(f"LocalObjectStore: key escapes root: {key!r}")
        path = self._root_path.joinpath(*parts)
        resolved_root = self._root_path.resolve()
        resolved = path.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise ValueError(f"LocalObjectStore: key escapes root: {key!r}")
        return path

    def get_object(self, key: str) -> _BytesObject:
        path = self._resolve(key)
        with self._lock:
            if not path.is_file():
                raise FileNotFoundError(f"LocalObjectStore: key not found: {key}")
            with open(path, "rb") as f:
                return _BytesObject(f.read(), key)

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        path = self._resolve(key)
        body = read_bytes(data)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: same-directory tmp file + os.replace, so concurrent
            # readers never observe a torn payload.
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def delete_object(self, key: str) -> None:
        path = self._resolve(key)
        with self._lock:
            try:
                os.remove(path)
            except FileNotFoundError:
                return

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        clean_prefix = str(prefix or "").replace("\\", "/").lstrip("/")
        with self._lock:
            if not self._root_path.exists():
                return iter(())
            keys: list[str] = []
            for path in self._root_path.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix == ".tmp":
                    continue
                rel = path.relative_to(self._root_path).as_posix()
                if rel.startswith(clean_prefix):
                    keys.append(rel)
        return iter(ObjectInfo(key) for key in sorted(keys))
