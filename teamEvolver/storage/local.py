"""Filesystem-backed object store."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Iterator

from .base import ObjectInfo, _BytesObject, read_bytes


class LocalObjectStore:
    """Filesystem-backed object store rooted at a local directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = str(Path(root).expanduser())

    def get_object(self, key: str) -> _BytesObject:
        path = os.path.join(self._root, key)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"LocalObjectStore: key not found: {key}")
        with open(path, "rb") as f:
            return _BytesObject(f.read(), key)

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        path = os.path.join(self._root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(read_bytes(data))

    def delete_object(self, key: str) -> None:
        path = os.path.join(self._root, key)
        if os.path.isfile(path):
            os.remove(path)

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        root = Path(self._root)
        if not root.exists():
            return iter(())
        keys: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith(prefix):
                keys.append(rel)
        return iter(ObjectInfo(key) for key in sorted(keys))
