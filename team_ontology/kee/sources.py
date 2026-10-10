"""Recursive generic OV reads; frozen text lives in KEE's private PG objects."""

import asyncio
import hashlib
import time
from urllib.parse import unquote, urlencode

from fastapi import HTTPException

from .contracts import digest, safe_uri

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".json", ".jsonl", ".csv"}


class Sources:
    def __init__(self, ops, structures):
        self.ops, self.db = ops, structures

    async def freeze(self, p, roots, uploads, readers):
        if len(readers) > 20:
            raise HTTPException(422, "SOURCE_READER_LIMIT")
        if p["subject"] not in readers:
            raise HTTPException(403, "SOURCE_OWNER_NOT_READER")
        cfg = self.ops.config
        settings = await self.ops.resolve(p)
        caller = settings.get("subject") or p["subject"]
        files, failures, seen, entries, size = [], [], set(), 0, 0
        started = time.monotonic()

        async def add(uri, text):
            nonlocal size
            raw = text.encode()
            if len(raw) > cfg.source_max_file_bytes:
                raise HTTPException(413, "SOURCE_FILE_SIZE_LIMIT")
            size += len(raw)
            if size > cfg.source_max_bytes or len(files) >= cfg.source_max_files:
                raise HTTPException(413, "SOURCE_COLLECTION_LIMIT")
            checksum = hashlib.sha256(raw).hexdigest()
            files.append(
                {
                    "source_id": digest({"uri": uri})[:32],
                    "uri": uri,
                    "revision": checksum,
                    "digest": checksum,
                    "text": text,
                    "readers": readers,
                }
            )

        async def walk(uri, depth=0):
            nonlocal entries
            if time.monotonic() - started > 110:
                raise HTTPException(408, "SOURCE_FREEZE_DEADLINE")
            try:
                uri = safe_uri(uri)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            if uri in seen:
                return
            seen.add(uri)
            if depth > cfg.source_max_depth:
                raise HTTPException(413, "SOURCE_DEPTH_LIMIT")
            stat = await self.ops.ov(p, "GET", "/fs/stat?" + urlencode({"uri": uri}), native=True, identity=caller)
            if not stat.get("isDir"):
                value = await self.ops.ov(
                    p, "GET", "/content/read?" + urlencode({"uri": uri}), native=True, identity=caller
                )
                text = value if isinstance(value, str) else value.get("content", "")
                await add(uri, text)
                return
            offset = 0
            while True:
                # Native ls returns a list and has_more outside result; fetch one full page plus sentinel.
                page = await self.ops.ov(
                    p,
                    "GET",
                    "/fs/ls?" + urlencode({"uri": uri, "offset": offset, "limit": 100, "output": "original"}),
                    native=True,
                    identity=caller,
                )
                items = page if isinstance(page, list) else page.get("entries", [])
                if not isinstance(items, list):
                    raise HTTPException(502, "OV_LIST_SHAPE_INVALID")
                entries += len(items)
                if entries > cfg.source_max_entries:
                    raise HTTPException(413, "SOURCE_ENTRY_LIMIT")
                for item in items:
                    child = item.get("uri") or (uri + "/" + item.get("name", ""))
                    name = unquote(child.rsplit("/", 1)[-1])
                    if (
                        not child.startswith(uri + "/")
                        or name.startswith(".")
                        or item.get("isLink")
                        or name in {"abstract.md", "overview.md"}
                    ):
                        continue
                    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if not item.get("isDir") and suffix not in SUPPORTED:
                        continue
                    try:
                        for attempt in range(3):
                            try:
                                await walk(child, depth + 1)
                                break
                            except HTTPException as exc:
                                if exc.status_code != 404 or attempt == 2:
                                    raise
                                seen.discard(child)
                                await asyncio.sleep(0.1 * (attempt + 1))
                    except HTTPException as exc:
                        if exc.status_code in {401, 403, 408, 413, 429, 500, 502, 503, 504}:
                            raise
                        failures.append({"uri": child, "code": str(exc.detail)})
                offset += len(items)
                if len(items) < 100:
                    break

        for root in roots:
            await walk(root)
        for upload in uploads:
            await add("upload:" + upload["name"], upload["text"])
        if not files:
            raise HTTPException(422, "NO_VALID_SOURCES")
        # Publication audience must already be able to read every original.
        await self.check_current(p, {"sources": files, "readers": readers})
        references = []
        for source in files:
            key = "source-revisions/" + digest(source)
            await self.db.put(p["tenant"], key, source, immutable=True)
            references.append({k: v for k, v in source.items() if k != "text"} | {"body_key": key})
        collection = {
            "sources": references,
            "failures": failures,
            "readers": readers,
            "snapshot": "per-file; not a simultaneous cross-source snapshot",
        }
        collection_id = digest(collection)
        await self.db.put(p["tenant"], "sources/" + collection_id, collection, immutable=True)
        return {"collection_id": collection_id, "count": len(files), "bytes": size, "failures": failures}

    async def load(self, p, collection_id):
        value = await self.db.get(p["tenant"], "sources/" + collection_id)
        if value is None:
            return None
        sources = []
        for ref in value["sources"]:
            source = await self.db.get(p["tenant"], ref["body_key"])
            if source is None or digest(source) != ref["body_key"].split("/")[-1]:
                raise HTTPException(409, "SOURCE_REVISION_CORRUPT")
            sources.append(source)
        return {**value, "sources": sources}

    async def check_current(self, p, collection, readers=None):
        # Re-read permission and state; retain frozen revision if the mutable source has changed.
        await self.ops.resolve(p)
        identities = await self.ops.kee.map_readers(p, readers if readers is not None else collection["readers"])
        started = time.monotonic()
        for source in collection["sources"]:
            if source["uri"].startswith("viking:"):
                for user in identities:
                    if time.monotonic() - started > 110:
                        raise HTTPException(408, "SOURCE_PERMISSION_CHECK_DEADLINE")
                    await self.ops.ov(
                        p, "GET", "/content/read?" + urlencode({"uri": source["uri"]}), native=True, identity=user
                    )
        return digest({"readers": collection["readers"], "sources": [s["digest"] for s in collection["sources"]]})
