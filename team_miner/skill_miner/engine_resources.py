"""Complete bounded source enumeration over the platform OpenViking client."""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException

from .ov_compile_mining import SOURCE_TAG_SKILL, normalize_source_tags, utc_now, validate_resource_uri
from .ov_round_orchestration_helpers import MAX_INPUT_FILES, MAX_SOURCE_FILE_BYTES

_LIST_PAGE_SIZE = 1000
_WIKI_ROOT = "viking://resources/agent_knowledge_workspace/output/processed_knowledge"


def _normalize_entries(result: Any, *, excluded_root: str = "") -> list[dict[str, Any]]:
    entries = result
    if isinstance(result, dict):
        entries = result.get("entries") or result.get("items") or []
    if not isinstance(entries, list):
        return []
    excluded = excluded_root.rstrip("/")
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        uri = str(raw.get("uri") or raw.get("path") or "").rstrip("/")
        if not uri or (excluded and (uri == excluded or uri.startswith(f"{excluded}/"))):
            continue
        normalized.append(
            {
                "uri": uri,
                "name": str(raw.get("name") or uri.rsplit("/", 1)[-1]),
                "is_dir": bool(raw.get("isDir", raw.get("is_dir", str(raw.get("uri") or "").endswith("/")))),
                "size": raw.get("size_bytes", raw.get("size")),
                "modified_at": raw.get("modTime", raw.get("mod_time", raw.get("modified_at", ""))),
                "abstract": str(raw.get("abstract") or ""),
            }
        )
    return sorted(normalized, key=lambda item: (item["uri"].count("/"), not item["is_dir"], item["name"].lower()))



class MiningResourceMixin:
    async def _iter_knowledge_entries(self, account: str, root: str, *, exclude_wiki: bool = False):
        """Walk visible original entries through the public, paginated single-directory API."""
        root = root.rstrip("/")
        # Depth-first iteration keeps only the current path and bounded pages;
        # neither a wide directory queue nor all source bodies live in memory.
        stack = [{"uri": root, "offset": 0, "page": [], "cursor": 0, "done": False, "last_key": None}]
        while stack:
            frame = stack[-1]
            if frame["cursor"] >= len(frame["page"]):
                if frame["done"]:
                    stack.pop()
                    continue
                result = await self._knowledge_request(
                    account,
                    "GET",
                    "/api/v1/fs/ls",
                    params={
                        "uri": frame["uri"],
                        "output": "original",
                        "recursive": "false",
                        "show_all_hidden": "false",
                        "node_limit": _LIST_PAGE_SIZE,
                        "offset": frame["offset"],
                        "sort_by": "name",
                        "sort_order": "asc",
                    },
                )
                page = result
                if isinstance(result, dict):
                    page = result.get("entries", result.get("items"))
                    if any(result.get(flag) for flag in ("truncated", "incomplete", "partial")):
                        raise HTTPException(
                            502, "OpenViking directory listing is incomplete; no complete source was claimed"
                        )
                if not isinstance(page, list) or len(page) > _LIST_PAGE_SIZE:
                    raise HTTPException(502, "OpenViking directory listing must be a bounded original entry list")
                normalized = []
                page_uris = set()
                for raw in page:
                    if not isinstance(raw, dict) or raw.get("access") == "denied":
                        raise HTTPException(403, "OpenViking source enumeration contains an inaccessible entry")
                    uri = raw.get("uri")
                    prefix = frame["uri"] + "/"
                    relative = uri[len(prefix) :] if isinstance(uri, str) and uri.startswith(prefix) else ""
                    if (
                        not relative
                        or "/" in relative.rstrip("/")
                        or "\\" in relative
                        or relative.rstrip("/") in {".", ".."}
                    ):
                        raise HTTPException(502, "OpenViking directory listing contains a non-child or invalid URI")
                    uri = uri.rstrip("/")
                    if uri in page_uris:
                        raise HTTPException(
                            502, "OpenViking source pagination repeated entries; enumeration is not complete"
                        )
                    page_uris.add(uri)
                    entries = _normalize_entries([raw])
                    if len(entries) != 1:
                        raise HTTPException(502, "OpenViking directory listing contains invalid metadata")
                    entry = entries[0]
                    key = (not entry["is_dir"], entry["name"].lower(), entry["name"])
                    if frame["last_key"] is not None and key <= frame["last_key"]:
                        raise HTTPException(502, "OpenViking source pagination is unstable or offset is unsupported")
                    frame["last_key"] = key
                    # OV hides generated files, while directory visibility can vary
                    # by backend. Hidden subtrees are outside the original-source contract.
                    if (
                        relative.startswith(".")
                        or exclude_wiki
                        and (uri == _WIKI_ROOT or uri.startswith(_WIKI_ROOT + "/"))
                    ):
                        continue
                    normalized.append(entry)
                frame.update(
                    page=normalized,
                    cursor=0,
                    done=len(page) < _LIST_PAGE_SIZE,
                    offset=frame["offset"] + len(page),
                )
                continue
            entry = frame["page"][frame["cursor"]]
            frame["cursor"] += 1
            yield entry
            if entry["is_dir"]:
                stack.append(
                    {"uri": entry["uri"], "offset": 0, "page": [], "cursor": 0, "done": False, "last_key": None}
                )


    async def _knowledge_tree(self, account: str, root: str, *, exclude_wiki: bool = False) -> dict[str, Any]:
        entries = []
        try:
            async for entry in self._iter_knowledge_entries(account, root, exclude_wiki=exclude_wiki):
                entries.append(entry)
        except HTTPException as exc:
            if not entries and (exc.status_code == 404 or "NOT_FOUND" in str(exc.detail)):
                return {"root_uri": root, "exists": False, "entries": []}
            raise
        return {
            "root_uri": root,
            "exists": True,
            "entries": _normalize_entries(entries),
            "enumeration_complete": True,
        }


    async def _ov_source_detail(self, record: dict) -> dict:
        account = str(record.get("account") or "")
        self._check_account(account)
        uri = validate_resource_uri(record.get("uri"))
        tree = await self._knowledge_tree(account, uri)
        entries = tree.get("entries") or []
        files = [entry for entry in entries if not entry.get("is_dir")]
        exists = bool(tree.get("exists"))
        status = "ready" if exists and files else "empty" if exists else "missing"
        total_bytes = sum(int(entry.get("size") or 0) for entry in files)
        error = "" if exists else "OpenViking 中的目录已被删除，请重新选择知识源目录"
        if status == "ready" and (
            len(files) > MAX_INPUT_FILES
            or any(int(row.get("size") or 0) > MAX_SOURCE_FILE_BYTES for row in files)
        ):
            status = "over_limit"
            error = "该来源超过 Skill 挖掘的单文件 20 MB 或目录 10000 个文件限额，请拆分超大的单文件或目录"
        return {
            "source_id": record["source_id"],
            "account": account,
            "path": uri,
            "uri": uri,
            "name": str(record.get("name") or uri.rsplit("/", 1)[-1]),
            "tags": normalize_source_tags(record.get("tags"), default=(SOURCE_TAG_SKILL,)),
            "default": bool(record.get("default")),
            "document_count": len(files),
            "total_bytes": total_bytes,
            "ready": status == "ready",
            "status": status,
            "created_at": record.get("created_at") or "",
            "ingestion": {
                "schema_version": 1,
                "source_path": uri,
                "batch_id": "",
                "status": "ready" if status == "ready" else "empty" if status == "empty" else "failed",
                "stage": "complete" if exists else "failed",
                "progress": 100,
                "processed_files": len(files),
                "total_files": len(files),
                "current_file": "",
                "error": error,
                "started_at": "",
                "updated_at": utc_now(),
                "finished_at": utc_now(),
            },
            "tree": tree,
        }
