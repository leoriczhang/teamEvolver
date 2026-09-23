"""Filesystem-backed object store for local disks and mounted NAS volumes.

This store implements the same object-store contract as
:class:`~teamEvolver.storage.viking.OpenVikingObjectStore`
(``get_object`` / ``put_object`` / ``delete_object`` / ``iter_objects``) on top
of a local directory. It can be selected directly for durable Skill storage,
or used as the fallback when the configured OpenViking deployment is
unavailable.

It intentionally does NOT define ``native_batch_write`` / ``batch_write``:
every batch caller already degrades to per-object writes when the attribute is
absent (see ``evolve/kernel/registry.py``, ``evolve/runtime/orchestrator.py``,
``validation/store.py``, ``dreamcycle/memory_changes.py``).

Data written here as an OpenViking fallback stays local; fallback data is not
automatically synchronized when the remote returns.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterator

from .base import ObjectInfo, _BytesObject, read_bytes

_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _root_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


class LocalObjectStore:
    """Filesystem-backed object store rooted at a local directory."""

    def __init__(self, root: str | Path) -> None:
        self._root_path = Path(root).expanduser()
        self._root = str(self._root_path)
        # All store instances targeting the same NAS/local root share one
        # in-process lock. Admin uploads and Evolution publish use separate
        # adapters and must not race on manifest/version files.
        self._lock = _root_lock(self._root_path)

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
            tmp_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    tmp_name = f.name
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, path)
            finally:
                if tmp_name:
                    try:
                        os.remove(tmp_name)
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
