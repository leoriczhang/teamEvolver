"""Deterministic per-user Memory snapshots for team aggregation.

Staging is intentionally model-free. It reads one user's visible Memory text,
serializes each source document as one JSONL record, and atomically publishes
the snapshot into the merge identity's private Resource space.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

import httpx

_STAGING_FORMAT = "teamevolver-memory-snapshot-v1"
# Per-directory listing bound. The tree is walked one directory at a time
# (non-recursive), so this caps entries within a single directory, not the
# whole memory tree. Total memory size is unbounded across directories.
_SOURCE_NODE_LIMIT = 10_000
_INSPECT_TIMEOUT_SECONDS = 60.0
_CHUNK_TARGET_BYTES = 4 * 1024 * 1024
_BATCH_TARGET_BYTES = 12 * 1024 * 1024
_BATCH_MAX_OPERATIONS = 200
_MAX_HTTP_OUTPUT_CHARS = 512 * 1024
_HTTP_RETRY_ATTEMPTS = 3
_HTTP_RETRY_BASE_SECONDS = 0.25


class StagingError(RuntimeError):
    """Raised when a deterministic Memory snapshot cannot be published."""


@dataclass(frozen=True)
class StagingSource:
    uri: str
    relative_path: str
    kind: str
    size: int
    modified_at: str


@dataclass(frozen=True)
class StagingInventory:
    user_id: str
    source_root: str
    kinds: tuple[str, ...]
    files: tuple[StagingSource, ...]
    fingerprint: str


@dataclass(frozen=True)
class StagingSnapshot:
    uri: str
    source_count: int
    total_bytes: int
    chunk_count: int
    reused: bool = False


@dataclass
class DeterministicStagingClient:
    """Copy one user's Memory into a private, immutable staging snapshot."""

    endpoint: str
    account_id: str
    source_user_id: str
    source_api_key: str
    target_user_id: str
    target_api_key: str
    agent_id: str = "team-skill-evolver"
    timeout_seconds: float = 3000.0

    def _headers(self, *, user_id: str, api_key: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "Authorization": f"Bearer {api_key}",
            "X-OpenViking-Account": self.account_id,
            "X-OpenViking-User": user_id,
            "X-OpenViking-Actor-Peer": self.agent_id,
        }

    @property
    def _source_headers(self) -> dict[str, str]:
        return self._headers(
            user_id=self.source_user_id,
            api_key=self.source_api_key,
        )

    @property
    def _target_headers(self) -> dict[str, str]:
        return self._headers(
            user_id=self.target_user_id,
            api_key=self.target_api_key,
        )

    def _client(self, *, timeout_seconds: float | None = None) -> httpx.AsyncClient:
        timeout = (
            max(1.0, timeout_seconds)
            if timeout_seconds is not None
            else max(30.0, self.timeout_seconds + 30.0)
        )
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        )

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(1, _HTTP_RETRY_ATTEMPTS + 1):
            try:
                request = getattr(client, method.lower())
                return await request(url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt >= _HTTP_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_HTTP_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
        raise RuntimeError("unreachable")

    @staticmethod
    def _payload(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return {}
        if (
            isinstance(payload, dict)
            and payload.get("status") in {None, "ok"}
            and "result" in payload
        ):
            return payload["result"]
        return payload

    @classmethod
    def _error_message(cls, response: httpx.Response) -> str:
        payload = cls._payload(response)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                if message:
                    return message[:_MAX_HTTP_OUTPUT_CHARS]
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if detail:
                return str(detail)[:_MAX_HTTP_OUTPUT_CHARS]
        return str(response.text or f"HTTP {response.status_code}")[
            :_MAX_HTTP_OUTPUT_CHARS
        ]

    @staticmethod
    def _relative_path(entry: dict[str, Any], source_root: str) -> str:
        relative = str(entry.get("rel_path") or "").strip("/")
        if not relative:
            relative = str(entry.get("uri") or "").removeprefix(source_root).strip("/")
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or any(part in {"", ".", ".."} or "\\" in part for part in path.parts)
        ):
            raise StagingError(f"unsafe source path returned by OpenViking: {relative!r}")
        return path.as_posix()

    @staticmethod
    def _kind_for_path(relative_path: str) -> str:
        first = relative_path.split("/", 1)[0]
        return first[:-3] if first.endswith(".md") else first

    async def _list_dir(
        self,
        client: httpx.AsyncClient,
        uri: str,
    ) -> list[dict[str, Any]]:
        """Return the immediate children of one directory (non-recursive).

        A missing directory (e.g. a requested kind the user never created) is
        treated as empty so the walk simply skips it.
        """
        try:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.endpoint.rstrip('/')}/api/v1/fs/ls",
                params={
                    "uri": uri,
                    "recursive": "false",
                    "node_limit": str(_SOURCE_NODE_LIMIT),
                    "output": "original",
                },
                headers=self._source_headers,
            )
        except httpx.HTTPError as exc:
            raise StagingError(f"OpenViking Memory inventory failed: {exc}") from exc
        if response.status_code == 404:
            return []
        if not response.is_success:
            raise StagingError(
                f"OpenViking Memory inventory failed: {self._error_message(response)}"
            )
        payload = self._payload(response)
        if not isinstance(payload, list):
            raise StagingError(
                "OpenViking Memory inventory returned an invalid response"
            )
        return payload

    async def inspect(self, kinds: Iterable[str]) -> StagingInventory:
        """Enumerate selected Memory files and compute a stable inventory hash.

        Enumeration walks the tree breadth-first with cheap **non-recursive**
        directory listings, descending only into requested kinds. A single
        recursive whole-tree listing is deliberately avoided: OpenViking's
        ``fs/ls`` has no pagination, so a recursive walk of a real user's
        memory forces a deep server-side traversal that times out (504). Per
        directory listings stay small and bounded no matter how large or deep
        the memory is, so arbitrarily large memory is copied in full.
        """
        selected = tuple(
            sorted({str(kind).strip() for kind in kinds if str(kind).strip()})
        )
        requested = set(selected)
        source_root = f"viking://user/{self.source_user_id}/memories"

        files: list[StagingSource] = []
        seen_uris: set[str] = set()
        async with self._client(timeout_seconds=_INSPECT_TIMEOUT_SECONDS) as client:
            # Start at the memory root and descend only into requested kinds.
            # The kind is the top-level path component, so this uniformly
            # handles a top-level file (``profile.md``) and a kind directory
            # (``events/...``) at any depth.
            pending: list[str] = [source_root]
            first_level = True
            while pending:
                results = await asyncio.gather(
                    *(self._list_dir(client, uri) for uri in pending)
                )
                pending = []
                for entries in results:
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        uri = str(entry.get("uri") or "").strip()
                        if not uri.startswith(f"{source_root}/"):
                            raise StagingError(
                                f"source URI escaped the Memory root: {uri!r}"
                            )
                        if uri in seen_uris:
                            continue
                        seen_uris.add(uri)
                        relative_path = self._relative_path(entry, source_root)
                        kind = self._kind_for_path(relative_path)
                        # Below the root every node already sits inside a
                        # requested kind; at the root, gate on the kind name.
                        if first_level and kind not in requested:
                            continue
                        if bool(entry.get("isDir")):
                            pending.append(uri)
                            continue
                        if PurePosixPath(relative_path).name.startswith("."):
                            continue
                        if kind not in requested:
                            continue
                        size = entry.get("size")
                        files.append(
                            StagingSource(
                                uri=uri,
                                relative_path=relative_path,
                                kind=kind,
                                size=int(size)
                                if isinstance(size, int) and size >= 0
                                else 0,
                                modified_at=str(entry.get("modTime") or ""),
                            )
                        )
                first_level = False
        files.sort(key=lambda item: (item.relative_path, item.uri))

        digest = hashlib.sha256()
        digest.update(f"{_STAGING_FORMAT}\0{self.source_user_id}".encode("utf-8"))
        for kind in selected:
            digest.update(b"\0kind\0")
            digest.update(kind.encode("utf-8"))
        for item in files:
            digest.update(b"\0file\0")
            digest.update(item.uri.encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(item.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(item.modified_at.encode("utf-8"))
        return StagingInventory(
            user_id=self.source_user_id,
            source_root=source_root,
            kinds=selected,
            files=tuple(files),
            fingerprint="sha256:" + digest.hexdigest(),
        )

    async def snapshot_exists(self, uri: str) -> bool:
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "GET",
                    f"{self.endpoint.rstrip('/')}/api/v1/fs/stat",
                    params={"uri": uri},
                    headers=self._target_headers,
                )
        except httpx.HTTPError as exc:
            raise StagingError(f"OpenViking staging stat failed: {exc}") from exc
        if response.status_code == 404:
            return False
        if not response.is_success:
            raise StagingError(
                f"OpenViking staging stat failed: {self._error_message(response)}"
            )
        payload = self._payload(response)
        return isinstance(payload, dict) and bool(payload.get("isDir"))

    async def publish(
        self,
        inventory: StagingInventory,
        *,
        staging_uri: str,
        run_id: str,
    ) -> StagingSnapshot:
        """Publish a complete immutable snapshot, returning only after atomic move."""
        if await self.snapshot_exists(staging_uri):
            return StagingSnapshot(
                uri=staging_uri,
                source_count=len(inventory.files),
                total_bytes=sum(item.size for item in inventory.files),
                chunk_count=0,
                reused=True,
            )

        suffix = "".join(char for char in run_id if char.isalnum() or char in "._-")
        suffix = suffix[-48:] or secrets.token_hex(8)
        pending_uri = f"{staging_uri}-pending-{suffix}"
        await self._mkdir(pending_uri)

        chunk_uris: list[str] = []
        pending_operations: list[dict[str, str]] = []
        pending_bytes = 0
        chunk_lines: list[str] = []
        chunk_bytes = 0
        total_bytes = 0
        content_digest = hashlib.sha256()

        async def flush_operations() -> None:
            nonlocal pending_bytes
            if not pending_operations:
                return
            await self._batch_write(pending_uri, list(pending_operations))
            pending_operations.clear()
            pending_bytes = 0

        async def flush_chunk() -> None:
            nonlocal chunk_bytes, pending_bytes
            if not chunk_lines:
                return
            content = "".join(chunk_lines)
            chunk_lines.clear()
            chunk_bytes = 0
            chunk_name = f"snapshot-{len(chunk_uris) + 1:05d}.jsonl"
            chunk_uri = f"{pending_uri}/{chunk_name}"
            chunk_uris.append(chunk_name)
            encoded_size = len(content.encode("utf-8"))
            if (
                pending_operations
                and (
                    len(pending_operations) >= _BATCH_MAX_OPERATIONS
                    or pending_bytes + encoded_size > _BATCH_TARGET_BYTES
                )
            ):
                await flush_operations()
            pending_operations.append(
                {"uri": chunk_uri, "content": content, "mode": "upsert"}
            )
            pending_bytes += encoded_size

        try:
            async with self._client() as client:
                for source in inventory.files:
                    response = await self._request_with_retry(
                        client,
                        "GET",
                        f"{self.endpoint.rstrip('/')}/api/v1/content/read",
                        params={"uri": source.uri},
                        headers=self._source_headers,
                    )
                    if not response.is_success:
                        raise StagingError(
                            f"OpenViking Memory read failed for {source.uri}: "
                            f"{self._error_message(response)}"
                        )
                    content = self._payload(response)
                    if not isinstance(content, str):
                        raise StagingError(
                            f"OpenViking Memory read returned non-text content: {source.uri}"
                        )
                    content_bytes = content.encode("utf-8")
                    total_bytes += len(content_bytes)
                    content_hash = hashlib.sha256(content_bytes).hexdigest()
                    content_digest.update(source.uri.encode("utf-8"))
                    content_digest.update(b"\0")
                    content_digest.update(content_hash.encode("ascii"))
                    content_digest.update(b"\0")
                    record = {
                        "source_uri": source.uri,
                        "relative_path": source.relative_path,
                        "kind": source.kind,
                        "modified_at": source.modified_at,
                        "content_sha256": content_hash,
                        "content": content,
                    }
                    line = json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
                    line_bytes = len(line.encode("utf-8"))
                    if line_bytes > _CHUNK_TARGET_BYTES:
                        raise StagingError(
                            "one Memory entry exceeds the deterministic staging "
                            f"chunk limit ({_CHUNK_TARGET_BYTES} bytes): {source.uri}"
                        )
                    if chunk_lines and chunk_bytes + line_bytes > _CHUNK_TARGET_BYTES:
                        await flush_chunk()
                    chunk_lines.append(line)
                    chunk_bytes += line_bytes

            await flush_chunk()
            await flush_operations()
            confirmed = await self.inspect(inventory.kinds)
            if confirmed.fingerprint != inventory.fingerprint:
                raise StagingError(
                    "user Memory changed while its deterministic snapshot was "
                    "being copied; retry the aggregation"
                )
            manifest = {
                "format": _STAGING_FORMAT,
                "source_user": inventory.user_id,
                "source_root": inventory.source_root,
                "source_fingerprint": inventory.fingerprint,
                "source_count": len(inventory.files),
                "total_content_bytes": total_bytes,
                "content_fingerprint": "sha256:" + content_digest.hexdigest(),
                "chunks": chunk_uris,
            }
            await self._batch_write(
                pending_uri,
                [
                    {
                        "uri": f"{pending_uri}/manifest.json",
                        "content": json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "mode": "upsert",
                    }
                ],
            )
            try:
                await self._move(pending_uri, staging_uri)
            except StagingError:
                if not await self.snapshot_exists(staging_uri):
                    raise
                await self._delete_best_effort(pending_uri)
                return StagingSnapshot(
                    uri=staging_uri,
                    source_count=len(inventory.files),
                    total_bytes=total_bytes,
                    chunk_count=len(chunk_uris),
                    reused=True,
                )
        except Exception:
            await self._delete_best_effort(pending_uri)
            raise

        return StagingSnapshot(
            uri=staging_uri,
            source_count=len(inventory.files),
            total_bytes=total_bytes,
            chunk_count=len(chunk_uris),
        )

    async def _mkdir(self, uri: str) -> None:
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "POST",
                    f"{self.endpoint.rstrip('/')}/api/v1/fs/mkdir",
                    json={"uri": uri},
                    headers=self._target_headers,
                )
        except httpx.HTTPError as exc:
            raise StagingError(f"OpenViking staging mkdir failed: {exc}") from exc
        if not response.is_success:
            raise StagingError(
                f"OpenViking staging mkdir failed: {self._error_message(response)}"
            )

    async def _batch_write(
        self,
        root_uri: str,
        operations: list[dict[str, str]],
    ) -> None:
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "POST",
                    f"{self.endpoint.rstrip('/')}/api/v1/content/batch-write",
                    json={
                        "root_uri": root_uri,
                        "operations": operations,
                        "wait": True,
                        "timeout": self.timeout_seconds,
                    },
                    headers=self._target_headers,
                )
        except httpx.HTTPError as exc:
            raise StagingError(f"OpenViking staging write failed: {exc}") from exc
        if not response.is_success:
            raise StagingError(
                f"OpenViking staging write failed: {self._error_message(response)}"
            )

    async def _move(self, from_uri: str, to_uri: str) -> None:
        try:
            async with self._client() as client:
                response = await self._request_with_retry(
                    client,
                    "POST",
                    f"{self.endpoint.rstrip('/')}/api/v1/fs/mv",
                    json={"from_uri": from_uri, "to_uri": to_uri},
                    headers=self._target_headers,
                )
        except httpx.HTTPError as exc:
            raise StagingError(f"OpenViking staging publish failed: {exc}") from exc
        if not response.is_success:
            raise StagingError(
                f"OpenViking staging publish failed: {self._error_message(response)}"
            )

    async def _delete_best_effort(self, uri: str) -> None:
        try:
            async with self._client() as client:
                await self._request_with_retry(
                    client,
                    "DELETE",
                    f"{self.endpoint.rstrip('/')}/api/v1/fs",
                    params={"uri": uri, "recursive": "true", "wait": "false"},
                    headers=self._target_headers,
                )
        except httpx.HTTPError:
            pass
