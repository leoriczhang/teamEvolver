"""Lossless, tenant-owned references for repeated immutable Session snapshots."""
from __future__ import annotations

import copy
import hashlib
import json

from .postgres import PostgresObjectStore

SNAPSHOT_REFERENCE = "$session_snapshot_object"
INLINE_SNAPSHOT_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class JsonSnapshots:
    """Pack JSON at storage boundaries; callers always consume complete JSON."""

    def __init__(self, tx, tenant_id):
        self.store = PostgresObjectStore(tx, tenant_id)
        self.loaded = {}
        self.written = set()

    async def _load(self, conn, reference):
        if not isinstance(reference, dict) or set(reference) != {"key", "sha256", "size"}:
            raise ValueError("Session snapshot reference mismatch")
        sha = reference["sha256"]
        if (
            not isinstance(sha, str) or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or reference["key"] != f"session-snapshots/{sha}.json"
            or type(reference["size"]) is not int
            or not 0 <= reference["size"] <= MAX_SNAPSHOT_BYTES
        ):
            raise ValueError("Session snapshot reference mismatch")
        if sha not in self.loaded:
            raw = await self.store.get(conn, reference["key"])
            if len(raw) != reference["size"] or hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("Session snapshot hash mismatch")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Session snapshot must be an object")
            self.loaded[sha] = value, len(raw)
        elif self.loaded[sha][1] != reference["size"]:
            raise ValueError("Session snapshot size mismatch")
        return self.loaded[sha][0]

    async def pack(self, conn, value):
        if isinstance(value, dict):
            if SNAPSHOT_REFERENCE in value:
                if set(value) != {SNAPSHOT_REFERENCE}:
                    raise ValueError("Session snapshot reference mismatch")
                # References supplied by a caller must resolve in this tenant.
                return await self._pack_snapshot(conn, await self._load(conn, value[SNAPSHOT_REFERENCE]))
            result = {}
            for key, item in value.items():
                result[key] = (
                    await self._pack_snapshot(conn, item)
                    if key == "session_snapshot" and isinstance(item, dict)
                    else await self.pack(conn, item)
                )
            return result
        if isinstance(value, list):
            return [await self.pack(conn, item) for item in value]
        return value

    async def _pack_snapshot(self, conn, snapshot):
        raw = canonical(snapshot).encode()
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Session snapshot exceeds 64 MiB")
        if len(raw) <= INLINE_SNAPSHOT_BYTES:
            return snapshot
        sha = hashlib.sha256(raw).hexdigest()
        key = f"session-snapshots/{sha}.json"
        if sha not in self.written:
            await self.store.put(conn, key, raw)
            self.written.add(sha)
        return {SNAPSHOT_REFERENCE: {"key": key, "sha256": sha, "size": len(raw)}}

    async def hydrate(self, conn, value):
        if isinstance(value, dict):
            if SNAPSHOT_REFERENCE in value:
                if set(value) != {SNAPSHOT_REFERENCE}:
                    raise ValueError("Session snapshot reference mismatch")
                return copy.deepcopy(await self._load(conn, value[SNAPSHOT_REFERENCE]))
            return {key: await self.hydrate(conn, item) for key, item in value.items()}
        if isinstance(value, list):
            return [await self.hydrate(conn, item) for item in value]
        return value
