"""OpenViking-backed knowledge-base mining routes for the console.

The feature deliberately keeps source documents and compiled Wiki pages in
OpenViking.  teamEvolver only brokers authenticated account selection,
uploads, compile task creation, status polling, and cancellation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import posixpath
import re
import uuid
from datetime import datetime, timezone
from html import unescape
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from team_memory.compile_client import CompileClient

from ..config_store import CONFIG_DIR
from ..storage import LocalObjectStore
from .knowledge_document_converter import convert_knowledge_upload
from .users_admin import (
    _require_admin_request,
    list_openviking_accounts,
)
from .wiki_graph import build_wiki_graph, render_wiki_graph_html

logger = logging.getLogger(__name__)

_RESOURCES_ROOT = "viking://resources"
_KNOWLEDGE_WORKSPACE_ROOT = f"{_RESOURCES_ROOT}/agent_knowledge_workspace"
_SOURCE_ROOT = f"{_KNOWLEDGE_WORKSPACE_ROOT}/input/raw_knowledge_base"
_WIKI_ROOT = f"{_KNOWLEDGE_WORKSPACE_ROOT}/output/processed_knowledge"
_KNOWLEDGE_MINING_ROOT = f"{_RESOURCES_ROOT}/knowledge-mining"
_DEFAULT_SKILL_URI = "viking://agent/skills/llm-wiki"
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,120}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MAX_UPLOAD_FILES = 200
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_TREE_NODES = 10_000
_MAX_CONTENT_BYTES = 2 * 1024 * 1024
_MAX_GRAPH_PAGES = 1_000
_MAX_GRAPH_CONTENT_BYTES = 16 * 1024 * 1024
_EDITABLE_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".js", ".ts"}
_WIKI_LINK_INDEX_VERSION = 1
_COMPILE_STATE_VERSION = 1
_COMPILE_SUCCESS_STATES = {"completed", "succeeded", "success"}
_COMPILE_TERMINAL_STATES = _COMPILE_SUCCESS_STATES | {"failed", "cancelled", "canceled"}
_COMPILE_STAGE_PROGRESS = {
    "queued": (5, "等待 OpenViking 调度"),
    "accepted": (5, "OpenViking 已受理"),
    "loading_skill": (12, "加载编译 Skill"),
    "collecting_context": (24, "扫描与读取知识源"),
    "source_coverage": (40, "分析来源覆盖"),
    "candidate_knowledge": (56, "提炼候选知识"),
    "page_generation": (72, "生成知识页面"),
    "agent": (64, "执行知识编译"),
    "rendering": (82, "渲染编译产物"),
    "writing": (91, "写入知识库"),
    "refreshing": (97, "刷新知识索引"),
    "salvaging": (88, "保存可恢复产物"),
    "salvaged": (100, "已保存可恢复产物"),
    "completed": (100, "编译完成"),
    "cancelled": (100, "任务已停止"),
    "canceled": (100, "任务已停止"),
    "failed": (100, "编译失败"),
}
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[([^\]\n]+)\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_MARKDOWN_REFERENCE_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\[([^\]\n]*)\]")
_MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
    r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(?:<([^>\n]+)>|([^\s]+))",
    re.MULTILINE,
)
_INLINE_CODE_RE = re.compile(r"(`{1,3})(?:(?!\1).)*\1")

_DEFAULT_WIKI_SKILL = """---
name: llm-wiki
description: Compile source documents into a navigable, evidence-grounded Wiki.
---

# Knowledge Wiki Compiler

Transform the selected source tree into a concise, navigable Markdown Wiki.

## Requirements

- Organize pages by stable concepts instead of mirroring file names.
- Preserve important facts, terminology, constraints, and procedures.
- Cite source Viking URIs near claims so readers can trace the evidence.
- Merge duplicates and surface conflicts explicitly; never invent missing facts.
- Use ordinary relative Markdown links between generated pages.
- Keep existing useful pages during incremental refreshes and update only what changed.
- Do not create semantic sidecar files such as `.abstract.md` or `.overview.md`.
"""


def _response_payload(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("status") == "ok" and "result" in payload:
        return payload["result"]
    return payload


def _error_message(response: httpx.Response, payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "").strip()
            if message:
                return message
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return (response.text or f"OpenViking HTTP {response.status_code}")[:1000]


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


def _safe_relative_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in raw.split("/") if part]
    if not parts or len(raw) > 500 or any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise HTTPException(status_code=400, detail=f"invalid upload path: {raw or '(empty)'}")
    return "/".join(parts)


def _knowledge_wiki_root(value: Any = None) -> str:
    """Return a normalized Wiki root contained by the OV resources namespace."""
    uri = str(value or _WIKI_ROOT).strip().rstrip("/")
    if (
        not uri.startswith(f"{_RESOURCES_ROOT}/")
        or len(uri) > 1000
        or any(part in {"", ".", ".."} for part in uri[len(_RESOURCES_ROOT) + 1 :].split("/"))
    ):
        raise HTTPException(status_code=400, detail="invalid Wiki root URI")
    return uri


def _knowledge_file_uri(kind: str, value: Any, *, wiki_root: str = _WIKI_ROOT) -> str:
    """Validate a file URI against the source or Wiki workspace boundary."""
    if kind not in {"source", "wiki"}:
        raise HTTPException(status_code=400, detail="kind must be source or wiki")
    uri = str(value or "").strip().rstrip("/")
    selected_wiki_root = _knowledge_wiki_root(wiki_root)
    root = _SOURCE_ROOT if kind == "source" else selected_wiki_root
    if not uri.startswith(f"{root}/") or len(uri) > 1000:
        raise HTTPException(status_code=400, detail=f"file must be below {root}")
    relative = uri[len(root) + 1 :]
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise HTTPException(status_code=400, detail="invalid knowledge file URI")
    if kind == "source" and (uri == selected_wiki_root or uri.startswith(f"{selected_wiki_root}/")):
        raise HTTPException(status_code=400, detail="wiki output is not part of the source workspace")
    return uri


def _knowledge_directory_uri(kind: str, value: Any, *, wiki_root: str = _WIKI_ROOT) -> str:
    """Validate an upload directory without allowing workspace escape."""
    if kind not in {"source", "wiki"}:
        raise HTTPException(status_code=400, detail="kind must be source or wiki")
    selected_wiki_root = _knowledge_wiki_root(wiki_root)
    root = _SOURCE_ROOT if kind == "source" else selected_wiki_root
    uri = str(value or root).strip().rstrip("/")
    if uri != root and not uri.startswith(f"{root}/"):
        raise HTTPException(status_code=400, detail=f"directory must be {root} or below it")
    if len(uri) > 1000:
        raise HTTPException(status_code=400, detail="knowledge directory URI is too long")
    relative = uri[len(root) :].lstrip("/")
    if relative and any(part in {"", ".", ".."} for part in relative.split("/")):
        raise HTTPException(status_code=400, detail="invalid knowledge directory URI")
    return uri


def _content_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("content") or result.get("text") or "")
    return str(result or "")


def _content_editable(uri: str) -> bool:
    filename = uri.rsplit("/", 1)[-1].lower()
    return any(filename.endswith(extension) for extension in _EDITABLE_EXTENSIONS)


def _knowledge_compile_progress(task: Any) -> dict[str, Any]:
    """Normalize OV Compile progress, preferring reported counters over stage estimates."""
    if not isinstance(task, dict):
        return {"percent": 0, "mode": "stage", "stage": "", "label": "等待任务状态"}

    status = str(task.get("status") or "").strip().lower()
    stage = str(task.get("stage") or status).strip().lower()
    progress = task.get("progress")
    percent: float | None = None
    if isinstance(progress, dict):
        raw_percent = progress.get("percent", progress.get("percentage"))
        try:
            if raw_percent is not None:
                percent = float(raw_percent)
            else:
                completed = float(progress.get("completed", progress.get("processed")))
                total = float(progress.get("total"))
                if total > 0:
                    percent = completed / total * 100
        except (TypeError, ValueError):
            percent = None
    elif isinstance(progress, (int, float)) and not isinstance(progress, bool):
        percent = float(progress)
        if 0 <= percent <= 1:
            percent *= 100

    if percent is not None:
        progress_details = progress if isinstance(progress, dict) else {}
        return {
            "percent": round(max(0.0, min(100.0, percent)), 1),
            "mode": "reported",
            "stage": stage,
            "label": str(progress_details.get("label") or progress_details.get("message") or stage or status),
        }

    fallback_percent, label = _COMPILE_STAGE_PROGRESS.get(
        stage,
        (8 if status in {"accepted", "pending", "queued"} else 50 if status in {"running", "committing"} else 0,
         stage or status or "等待任务状态"),
    )
    if status in _COMPILE_SUCCESS_STATES:
        fallback_percent, label = 100, "编译完成"
    return {
        "percent": fallback_percent,
        "mode": "stage",
        "stage": stage,
        "label": label,
    }


def _markdown_without_code(content: str) -> str:
    """Blank fenced and inline code while preserving line numbers."""
    output: list[str] = []
    fence = ""
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        marker_match = re.match(r"(`{3,}|~{3,})", stripped)
        marker = marker_match.group(1) if marker_match else ""
        if fence:
            output.append("\n" if line.endswith("\n") else "")
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if marker:
            fence = marker
            output.append("\n" if line.endswith("\n") else "")
            continue
        output.append(_INLINE_CODE_RE.sub("", line))
    return "".join(output)


def _extract_markdown_links(content: str) -> list[dict[str, Any]]:
    """Return ordinary Markdown links, excluding images and code samples."""
    searchable = _markdown_without_code(content)
    definitions: dict[str, str] = {}
    for match in _MARKDOWN_REFERENCE_DEFINITION_RE.finditer(searchable):
        label = match.group(1).strip().casefold()
        href = (match.group(2) or match.group(3) or "").strip()
        if label and href:
            definitions[label] = href

    links: list[dict[str, Any]] = []
    for match in _MARKDOWN_LINK_RE.finditer(searchable):
        links.append(
            {
                "label": match.group(1).strip(),
                "href": (match.group(2) or match.group(3) or "").strip(),
                "line": searchable.count("\n", 0, match.start()) + 1,
            }
        )
    for match in _MARKDOWN_REFERENCE_LINK_RE.finditer(searchable):
        reference = (match.group(2) or match.group(1)).strip().casefold()
        href = definitions.get(reference, "")
        if href:
            links.append(
                {
                    "label": match.group(1).strip(),
                    "href": href,
                    "line": searchable.count("\n", 0, match.start()) + 1,
                }
            )
    return links


def _resolve_wiki_link(
    source_uri: str,
    href: str,
    page_uris: set[str],
    *,
    wiki_root: str = _WIKI_ROOT,
) -> str:
    """Resolve a Markdown href to an existing Wiki page URI, or return empty."""
    value = unescape(unquote(str(href or "").strip()))
    if not value or value.startswith("#"):
        return ""
    path = value.split("#", 1)[0].split("?", 1)[0].strip()
    if not path:
        return ""
    lowered = path.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "data:", "javascript:")):
        return ""
    if path.startswith("viking://"):
        candidate = path.rstrip("/")
    else:
        source_relative = source_uri[len(wiki_root) :].lstrip("/")
        if path.startswith("/"):
            relative = path.lstrip("/")
        else:
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(source_relative), path))
        if relative == ".." or relative.startswith("../"):
            return ""
        candidate = f"{wiki_root}/{relative.lstrip('/')}".rstrip("/")

    candidates = [candidate]
    if not PurePosixPath(candidate).suffix:
        candidates.extend([f"{candidate}.md", f"{candidate}/index.md"])
    return next((item for item in candidates if item in page_uris), "")


def _build_wiki_link_index(
    documents: dict[str, str],
    *,
    account: str = "",
    compile_task_id: str = "",
    wiki_root: str = _WIKI_ROOT,
) -> dict[str, Any]:
    """Build deterministic outgoing links and backlinks for the complete Wiki."""
    page_uris = set(documents)
    pages = {
        uri: {
            "uri": uri,
            "name": uri.rsplit("/", 1)[-1],
            "path": uri[len(wiki_root) :].lstrip("/"),
            "links": [],
            "backlinks": [],
        }
        for uri in sorted(page_uris)
    }
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    occurrence_count = 0
    for source_uri, content in sorted(documents.items()):
        for link in _extract_markdown_links(content):
            target_uri = _resolve_wiki_link(
                source_uri,
                str(link["href"]),
                page_uris,
                wiki_root=wiki_root,
            )
            if not target_uri or target_uri == source_uri:
                continue
            occurrence_count += 1
            edge = edges.setdefault(
                (source_uri, target_uri),
                {
                    "source_uri": source_uri,
                    "source_name": source_uri.rsplit("/", 1)[-1],
                    "source_path": source_uri[len(wiki_root) :].lstrip("/"),
                    "target_uri": target_uri,
                    "target_name": target_uri.rsplit("/", 1)[-1],
                    "target_path": target_uri[len(wiki_root) :].lstrip("/"),
                    "labels": [],
                    "lines": [],
                    "count": 0,
                },
            )
            label = str(link["label"] or "").strip()
            line = int(link["line"])
            if label and label not in edge["labels"]:
                edge["labels"].append(label)
            if line not in edge["lines"]:
                edge["lines"].append(line)
            edge["count"] += 1

    for edge in edges.values():
        pages[edge["source_uri"]]["links"].append(
            {key: value for key, value in edge.items() if not key.startswith("source_")}
        )
        pages[edge["target_uri"]]["backlinks"].append(
            {key: value for key, value in edge.items() if not key.startswith("target_")}
        )
    for page in pages.values():
        page["links"].sort(key=lambda item: (item["target_path"].casefold(), item["target_uri"]))
        page["backlinks"].sort(key=lambda item: (item["source_path"].casefold(), item["source_uri"]))

    return {
        "version": _WIKI_LINK_INDEX_VERSION,
        "account": account,
        "wiki_root": wiki_root,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compile_task_id": compile_task_id,
        "page_count": len(pages),
        "edge_count": len(edges),
        "link_count": occurrence_count,
        "pages": pages,
    }


class KnowledgeMiningMixin:
    """Routes for compiling one OpenViking account's Resources into a Wiki."""

    def _knowledge_compile_store(self) -> LocalObjectStore:
        store = getattr(self, "_knowledge_compile_state_store", None)
        if store is None:
            store = LocalObjectStore(CONFIG_DIR / "knowledge_compile_state")
            self._knowledge_compile_state_store = store
        return store

    def _knowledge_compile_scope_key(
        self,
        account: str,
        source_uri: str,
        target_uri: str,
        skill_uri: str,
    ) -> str:
        identity = f"{self._knowledge_endpoint()}|{account}|{source_uri}|{target_uri}|{skill_uri}"
        return f"watermark-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"

    def _knowledge_compile_receipt_key(self, account: str, task_id: str) -> str:
        identity = f"{self._knowledge_endpoint()}|{account}|{task_id}"
        return f"task-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"

    def _load_knowledge_compile_json(self, key: str) -> dict[str, Any] | None:
        try:
            payload = self._knowledge_compile_store().get_object(key).read()
            document = json.loads(payload.decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
            return None
        return document if isinstance(document, dict) and document.get("version") == _COMPILE_STATE_VERSION else None

    def _save_knowledge_compile_json(self, key: str, document: dict[str, Any]) -> None:
        self._knowledge_compile_store().put_object(
            key,
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def _knowledge_last_compile_time(
        self,
        account: str,
        source_uri: str = _SOURCE_ROOT,
        target_uri: str = _WIKI_ROOT,
        skill_uri: str = _DEFAULT_SKILL_URI,
    ) -> str:
        state = self._load_knowledge_compile_json(
            self._knowledge_compile_scope_key(account, source_uri, target_uri, skill_uri)
        )
        return str((state or {}).get("last_compile_time") or "")

    def _record_knowledge_compile_submission(
        self,
        account: str,
        task_id: str,
        *,
        compile_started_at: str,
        source_uri: str,
        target_uri: str,
        skill_uri: str,
        last_compile_time: str,
    ) -> None:
        receipt = {
            "version": _COMPILE_STATE_VERSION,
            "endpoint": self._knowledge_endpoint(),
            "account": account,
            "task_id": task_id,
            "status": "accepted",
            "compile_started_at": compile_started_at,
            "last_compile_time": last_compile_time,
            "source_uri": source_uri,
            "target_uri": target_uri,
            "skill_uri": skill_uri,
        }
        self._save_knowledge_compile_json(
            self._knowledge_compile_receipt_key(account, task_id),
            receipt,
        )

    def _finalize_knowledge_compile(self, account: str, task_id: str, status: str) -> None:
        """Advance the incremental watermark only after a successful Compile task."""
        receipt_key = self._knowledge_compile_receipt_key(account, task_id)
        receipt = self._load_knowledge_compile_json(receipt_key)
        if receipt is None:
            return
        normalized_status = str(status or "").lower()
        if normalized_status not in _COMPILE_TERMINAL_STATES:
            return
        if receipt.get("status") == normalized_status:
            return

        receipt["status"] = normalized_status
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if normalized_status in _COMPILE_SUCCESS_STATES:
            source_uri = str(receipt.get("source_uri") or _SOURCE_ROOT)
            target_uri = str(receipt.get("target_uri") or _WIKI_ROOT)
            skill_uri = str(receipt.get("skill_uri") or _DEFAULT_SKILL_URI)
            compile_started_at = str(receipt.get("compile_started_at") or "")
            scope_key = self._knowledge_compile_scope_key(account, source_uri, target_uri, skill_uri)
            current = self._load_knowledge_compile_json(scope_key) or {}
            previous = str(current.get("last_compile_time") or "")
            if compile_started_at and compile_started_at > previous:
                self._save_knowledge_compile_json(
                    scope_key,
                    {
                        "version": _COMPILE_STATE_VERSION,
                        "endpoint": self._knowledge_endpoint(),
                        "account": account,
                        "source_uri": source_uri,
                        "target_uri": target_uri,
                        "skill_uri": skill_uri,
                        "last_compile_time": compile_started_at,
                        "last_successful_task_id": task_id,
                        "updated_at": receipt["finished_at"],
                    },
                )
        self._save_knowledge_compile_json(receipt_key, receipt)

    def _knowledge_link_store(self) -> LocalObjectStore:
        store = getattr(self, "_knowledge_link_index_store", None)
        if store is None:
            store = LocalObjectStore(CONFIG_DIR / "knowledge_link_indexes")
            self._knowledge_link_index_store = store
        return store

    def _knowledge_link_index_key(self, account: str, wiki_root: str = _WIKI_ROOT) -> str:
        identity = f"{self._knowledge_endpoint()}|{account}|{wiki_root}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{digest}.json"

    def _load_knowledge_link_index(
        self,
        account: str,
        wiki_root: str = _WIKI_ROOT,
    ) -> dict[str, Any] | None:
        try:
            payload = self._knowledge_link_store().get_object(
                self._knowledge_link_index_key(account, wiki_root)
            ).read()
            index = json.loads(payload.decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
            return None
        if not isinstance(index, dict) or index.get("version") != _WIKI_LINK_INDEX_VERSION:
            return None
        if index.get("account") != account or index.get("wiki_root") != wiki_root:
            return None
        return index

    def _save_knowledge_link_index(
        self,
        account: str,
        index: dict[str, Any],
        wiki_root: str = _WIKI_ROOT,
    ) -> None:
        self._knowledge_link_store().put_object(
            self._knowledge_link_index_key(account, wiki_root),
            json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def _knowledge_link_lock(self, account: str, wiki_root: str = _WIKI_ROOT) -> asyncio.Lock:
        locks = getattr(self, "_knowledge_link_index_locks", None)
        if locks is None:
            locks = {}
            self._knowledge_link_index_locks = locks
        key = (id(asyncio.get_running_loop()), account, wiki_root)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _knowledge_config(self):
        workspace_config = getattr(self, "_workspace_config", None)
        return workspace_config() if callable(workspace_config) else self.config

    def _knowledge_endpoint(self) -> str:
        config = self._knowledge_config()
        return str(getattr(config, "sharing_viking_endpoint", "") or "").strip().rstrip("/")

    def _knowledge_api_key(self) -> str:
        config = self._knowledge_config()
        return str(
            getattr(config, "sharing_viking_team_api_key", "")
            or getattr(config, "sharing_viking_api_key", "")
            or getattr(config, "sharing_viking_personal_api_key", "")
            or ""
        ).strip()

    def _knowledge_team_user(self) -> str:
        return str(getattr(self._knowledge_config(), "sharing_viking_user", "") or "team").strip() or "team"

    def _knowledge_accounts(self) -> dict[str, Any]:
        return list_openviking_accounts(self._knowledge_config())

    def _knowledge_account(self, value: Any) -> str:
        account = str(value or "").strip()
        if not _ACCOUNT_RE.fullmatch(account):
            raise HTTPException(status_code=400, detail="invalid OpenViking account")
        return account

    def _knowledge_headers(self, account: str, *, shared_admin: bool = False) -> dict[str, str]:
        api_key = self._knowledge_api_key()
        deployment = str(
            getattr(self._knowledge_config(), "sharing_viking_deployment", "") or "cloud"
        ).strip().lower()
        if not api_key and deployment != "local":
            raise HTTPException(status_code=503, detail="OpenViking service key is not configured")
        headers = {
            "Accept": "application/json",
            "X-OpenViking-Account": account,
            "X-OpenViking-User": self._knowledge_team_user(),
            "X-OpenViking-Agent": "team-skill-evolver",
        }
        # Send the configured key for every deployment mode: trusted ("local")
        # servers require the root key on each request, while dev/api_key
        # servers simply ignore or validate it — never harmful, always required.
        if api_key:
            headers["X-API-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"
        if shared_admin:
            headers["X-OpenViking-Role"] = "admin"
        return headers

    _knowledge_http_pool: httpx.AsyncClient | None = None

    def _knowledge_http(self) -> httpx.AsyncClient:
        """Shared keep-alive AsyncClient for all OpenViking requests.

        A fresh client per request forced a new DNS lookup + TCP handshake on
        every OV call, which is prohibitively slow when the endpoint hostname
        resolves slowly (e.g. macOS .local mDNS adds a fixed 5s penalty).
        Connections are pooled per origin and reused across requests.
        """
        client = self._knowledge_http_pool
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=60.0)
            self._knowledge_http_pool = client
        return client

    async def aclose_knowledge_http(self) -> None:
        client, self._knowledge_http_pool = self._knowledge_http_pool, None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def _knowledge_request(
        self,
        account: str,
        method: str,
        path: str,
        *,
        timeout: float = 60.0,
        shared_admin: bool = False,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        endpoint = self._knowledge_endpoint()
        if not endpoint:
            raise HTTPException(status_code=503, detail="OpenViking endpoint is not configured")
        headers = self._knowledge_headers(account, shared_admin=shared_admin)
        if extra_headers:
            headers.update(extra_headers)
        response: httpx.Response | None = None
        last_error: httpx.HTTPError | None = None
        for _attempt in range(2):
            try:
                response = await self._knowledge_http().request(
                    method, f"{endpoint}{path}", headers=headers, timeout=timeout, **kwargs
                )
                break
            except (httpx.NetworkError, httpx.ProtocolError) as exc:
                # Pooled keep-alive connections can silently go stale (the server
                # or a middlebox closed/reset them): drop the pool and retry once
                # on a fresh connection. Writes through this path are idempotent
                # whole-content replaces, so a retry cannot double-apply an edit.
                last_error = exc
                await self.aclose_knowledge_http()
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=503, detail=f"OpenViking is unreachable: {exc}") from exc
        if response is None:
            raise HTTPException(status_code=503, detail=f"OpenViking is unreachable: {last_error}") from last_error
        payload = _response_payload(response)
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=_error_message(response, payload))
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise HTTPException(status_code=502, detail=_error_message(response, payload))
        return payload

    async def _knowledge_tree(self, account: str, root: str, *, exclude_wiki: bool = False) -> dict[str, Any]:
        try:
            result = await self._knowledge_request(
                account,
                "GET",
                "/api/v1/fs/tree",
                params={
                    "uri": root,
                    "output": "original",
                    "show_all_hidden": "false",
                    "level_limit": 24,
                    "node_limit": _MAX_TREE_NODES,
                    "sort_by": "name",
                    "sort_order": "asc",
                },
            )
        except HTTPException as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc.detail):
                return {"root_uri": root, "exists": False, "entries": []}
            raise
        return {
            "root_uri": root,
            "exists": True,
            "entries": _normalize_entries(result, excluded_root=_WIKI_ROOT if exclude_wiki else ""),
        }

    async def _knowledge_wiki_roots(self, account: str) -> list[dict[str, Any]]:
        """Discover non-empty Wiki outputs produced by historical OV Compile runs."""
        try:
            result = await self._knowledge_request(
                account,
                "GET",
                "/api/v1/fs/tree",
                params={
                    "uri": _KNOWLEDGE_MINING_ROOT,
                    "output": "original",
                    "show_all_hidden": "false",
                    "level_limit": 24,
                    "node_limit": _MAX_TREE_NODES,
                    "sort_by": "name",
                    "sort_order": "asc",
                },
            )
        except HTTPException as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc.detail):
                return []
            raise
        entries = _normalize_entries(result)
        roots: dict[str, dict[str, Any]] = {}
        for entry in entries:
            uri = str(entry["uri"])
            match = re.match(rf"^({re.escape(_KNOWLEDGE_MINING_ROOT)}/[^/]+/wiki)(?:/|$)", uri)
            if not match:
                continue
            root = match.group(1)
            item = roots.setdefault(
                root,
                {
                    "uri": root,
                    "name": root[len(_KNOWLEDGE_MINING_ROOT) + 1 : -len("/wiki")],
                    "page_count": 0,
                    "modified_at": "",
                    "abstract": "",
                },
            )
            if entry["is_dir"] and uri == root:
                item["abstract"] = entry.get("abstract") or ""
            elif not entry["is_dir"] and uri.lower().endswith((".md", ".markdown", ".mdx")):
                item["page_count"] += 1
            item["modified_at"] = max(str(item["modified_at"]), str(entry.get("modified_at") or ""))
        return sorted(
            (item for item in roots.values() if item["page_count"]),
            key=lambda item: (str(item["modified_at"]), int(item["page_count"])),
            reverse=True,
        )

    async def _knowledge_compile_capabilities(self, account: str) -> dict[str, Any]:
        """Read Compile readiness, tolerating OV deployments predating the probe API."""
        try:
            capabilities = await self._knowledge_request(
                account,
                "GET",
                "/api/v1/compile/capabilities",
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return {
                    "configured": None,
                    "can_create": True,
                    "probe_supported": False,
                    "reason_code": "CAPABILITY_PROBE_UNAVAILABLE",
                }
            raise
        if not isinstance(capabilities, dict):
            raise HTTPException(status_code=502, detail="OpenViking returned invalid Compile capabilities")
        return {**capabilities, "probe_supported": True}

    async def _knowledge_rebuild_link_index(
        self,
        account: str,
        *,
        compile_task_id: str = "",
        force: bool = False,
        wiki_root: str = _WIKI_ROOT,
    ) -> dict[str, Any]:
        """Scan every Wiki Markdown page and atomically replace its link index."""
        async with self._knowledge_link_lock(account, wiki_root):
            existing = await asyncio.to_thread(self._load_knowledge_link_index, account, wiki_root)
            processed_task_ids = (
                list(existing.get("processed_compile_task_ids", []))
                if existing and isinstance(existing.get("processed_compile_task_ids"), list)
                else []
            )
            if not force and compile_task_id and existing and compile_task_id in processed_task_ids:
                return existing

            documents = await self._knowledge_read_wiki_documents(account, wiki_root=wiki_root)
            index = _build_wiki_link_index(
                documents,
                account=account,
                compile_task_id=compile_task_id or str((existing or {}).get("compile_task_id") or ""),
                wiki_root=wiki_root,
            )
            if compile_task_id and compile_task_id not in processed_task_ids:
                processed_task_ids.append(compile_task_id)
            index["processed_compile_task_ids"] = processed_task_ids[-100:]
            await asyncio.to_thread(self._save_knowledge_link_index, account, index, wiki_root)
            return index

    async def _knowledge_read_wiki_documents(
        self,
        account: str,
        *,
        max_pages: int | None = None,
        wiki_root: str = _WIKI_ROOT,
    ) -> dict[str, str]:
        """Read the complete compiled Wiki through authenticated OV APIs."""
        tree = await self._knowledge_tree(account, wiki_root)
        markdown_uris = sorted(
            entry["uri"]
            for entry in tree["entries"]
            if not entry["is_dir"] and str(entry["uri"]).lower().endswith((".md", ".markdown", ".mdx"))
        )
        if max_pages is not None and len(markdown_uris) > max_pages:
            raise HTTPException(
                status_code=413,
                detail=f"Wiki graph supports at most {max_pages} Markdown pages",
            )
        read_slots = asyncio.Semaphore(8)

        async def read_page(uri: str) -> tuple[str, str]:
            async with read_slots:
                result = await self._knowledge_request(
                    account,
                    "GET",
                    "/api/v1/content/read",
                    params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"},
                )
                return uri, _content_text(result)

        return dict(await asyncio.gather(*(read_page(uri) for uri in markdown_uris)))

    @staticmethod
    def _knowledge_link_index_summary(index: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ready",
            "generated_at": index.get("generated_at"),
            "page_count": index.get("page_count", 0),
            "edge_count": index.get("edge_count", 0),
            "link_count": index.get("link_count", 0),
        }

    async def _knowledge_page_links(
        self,
        account: str,
        uri: str,
        *,
        wiki_root: str = _WIKI_ROOT,
    ) -> dict[str, Any]:
        index = await asyncio.to_thread(self._load_knowledge_link_index, account, wiki_root)
        if index is None:
            # Building the first link index reads every Markdown page from OV.
            # Doing that on the file-open request makes a single remote read
            # look as slow as opening the whole Wiki.  Return the document now
            # and populate backlinks in the background for subsequent opens.
            self._start_knowledge_link_rebuild(account, wiki_root=wiki_root)
            return {
                "links": [],
                "backlinks": [],
                "link_index": {"status": "pending"},
            }
        page = index.get("pages", {}).get(uri, {}) if isinstance(index.get("pages"), dict) else {}
        return {
            "links": page.get("links", []) if isinstance(page, dict) else [],
            "backlinks": page.get("backlinks", []) if isinstance(page, dict) else [],
            "link_index": self._knowledge_link_index_summary(index),
        }

    async def _knowledge_rebuild_links_in_background(
        self,
        account: str,
        *,
        wiki_root: str,
        delay: float = 0.0,
    ) -> None:
        """Refresh one Wiki link index without extending an interactive request."""
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._knowledge_rebuild_link_index(
                account,
                force=True,
                wiki_root=wiki_root,
            )
        except Exception as exc:  # noqa: BLE001 - content reads/writes already succeeded
            logger.warning("Cannot rebuild Wiki link index for %s: %s", wiki_root, exc)

    def _start_knowledge_link_rebuild(
        self,
        account: str,
        *,
        wiki_root: str = _WIKI_ROOT,
        delay: float = 0.0,
    ) -> None:
        """Start at most one background link-index refresh per account/root."""
        rebuilds = getattr(self, "_knowledge_link_rebuilds", None)
        if rebuilds is None:
            rebuilds = {}
            self._knowledge_link_rebuilds = rebuilds
        key = (account, wiki_root)
        current = rebuilds.get(key)
        if current is not None and not current.done():
            return
        create_task = getattr(self, "_safe_create_task", None)
        coroutine = self._knowledge_rebuild_links_in_background(
            account,
            wiki_root=wiki_root,
            delay=delay,
        )
        task = create_task(coroutine) if callable(create_task) else asyncio.create_task(coroutine)
        rebuilds[key] = task

        def forget(completed: asyncio.Task) -> None:
            if rebuilds.get(key) is completed:
                rebuilds.pop(key, None)

        task.add_done_callback(forget)

    async def _knowledge_watch_compile(self, account: str, task_id: str) -> None:
        """Wait for one accepted Compile task, then rebuild the whole Wiki graph."""
        deadline = asyncio.get_running_loop().time() + 3600
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.5)
            try:
                task = await self._knowledge_request(
                    account,
                    "GET",
                    f"/api/v1/tasks/{task_id}",
                    params={"include_events": "false"},
                )
            except HTTPException as exc:
                logger.warning("Cannot poll Compile task %s for Wiki link rebuild: %s", task_id, exc.detail)
                continue
            status = str(task.get("status") or "").lower() if isinstance(task, dict) else ""
            if status in _COMPILE_SUCCESS_STATES:
                await asyncio.to_thread(self._finalize_knowledge_compile, account, task_id, status)
                await self._knowledge_rebuild_link_index(account, compile_task_id=task_id)
                return
            if status in _COMPILE_TERMINAL_STATES:
                await asyncio.to_thread(self._finalize_knowledge_compile, account, task_id, status)
                return
        logger.warning("Timed out waiting for Compile task %s before Wiki link rebuild", task_id)

    def _start_knowledge_compile_watch(self, account: str, task_id: str) -> None:
        watchers = getattr(self, "_knowledge_compile_watchers", None)
        if watchers is None:
            watchers = {}
            self._knowledge_compile_watchers = watchers
        key = (account, task_id)
        current = watchers.get(key)
        if current is not None and not current.done():
            return
        create_task = getattr(self, "_safe_create_task", None)
        task = (
            create_task(self._knowledge_watch_compile(account, task_id))
            if callable(create_task)
            else asyncio.create_task(self._knowledge_watch_compile(account, task_id))
        )
        watchers[key] = task
        task.add_done_callback(lambda completed: watchers.pop(key, None))

    def _register_knowledge_mining_routes(self, app: FastAPI) -> None:
        owner = self

        @app.get("/api/knowledge-mining/config")
        async def knowledge_mining_config(request: Request):
            _require_admin_request(request)
            accounts = await asyncio.to_thread(owner._knowledge_accounts)
            return JSONResponse(
                {
                    "accounts": accounts.get("accounts") or [],
                    "current": accounts.get("current") or "",
                    "source_root": _SOURCE_ROOT,
                    "wiki_root": _WIKI_ROOT,
                    "skill_uri": _DEFAULT_SKILL_URI,
                    "endpoint": owner._knowledge_endpoint(),
                    "account_source": accounts.get("source") or "fallback",
                    "error": accounts.get("error") or "",
                }
            )

        @app.get("/api/knowledge-mining/tree")
        async def knowledge_mining_tree(
            request: Request,
            account: str = Query(...),
            kind: str = Query(...),
            wiki_root: str = Query(_WIKI_ROOT),
        ):
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            if kind not in {"source", "wiki"}:
                raise HTTPException(status_code=400, detail="kind must be source or wiki")
            selected_wiki_root = _knowledge_wiki_root(wiki_root)
            tree = await owner._knowledge_tree(
                selected,
                _SOURCE_ROOT if kind == "source" else selected_wiki_root,
                exclude_wiki=kind == "source",
            )
            return JSONResponse({"account": selected, "kind": kind, **tree})

        @app.get("/api/knowledge-mining/wiki-roots")
        async def knowledge_mining_wiki_roots(request: Request, account: str = Query(...)):
            """List non-empty Wiki roots from previous OV Compile runs."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            roots = await owner._knowledge_wiki_roots(selected)
            return JSONResponse(
                {
                    "account": selected,
                    "default": _WIKI_ROOT,
                    "roots": roots,
                }
            )

        @app.get("/api/knowledge-mining/capabilities")
        async def knowledge_mining_capabilities(request: Request, account: str = Query(...)):
            """Expose OV's account-scoped Compile readiness to the console."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            capabilities = await owner._knowledge_compile_capabilities(selected)
            last_compile_time = await asyncio.to_thread(
                owner._knowledge_last_compile_time,
                selected,
                _SOURCE_ROOT,
                _WIKI_ROOT,
                _DEFAULT_SKILL_URI,
            )
            return JSONResponse(
                {
                    "account": selected,
                    **capabilities,
                    "last_compile_time": last_compile_time or None,
                    "incremental": bool(last_compile_time),
                }
            )

        @app.post("/api/knowledge-mining/graph")
        async def knowledge_mining_graph(request: Request, body: dict[str, Any]):
            """Read the complete Wiki from OV and return its sandboxable HTML graph."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            wiki_root = _knowledge_wiki_root(body.get("wiki_root"))
            documents = await owner._knowledge_read_wiki_documents(
                selected,
                max_pages=_MAX_GRAPH_PAGES,
                wiki_root=wiki_root,
            )
            if not documents:
                raise HTTPException(status_code=404, detail="the compiled Wiki has no Markdown pages")
            total_bytes = sum(len(content.encode("utf-8")) for content in documents.values())
            if total_bytes > _MAX_GRAPH_CONTENT_BYTES:
                raise HTTPException(status_code=413, detail="Wiki graph content exceeds 16 MB")

            previous = await asyncio.to_thread(owner._load_knowledge_link_index, selected, wiki_root)
            index = _build_wiki_link_index(
                documents,
                account=selected,
                compile_task_id=str((previous or {}).get("compile_task_id") or ""),
                wiki_root=wiki_root,
            )
            if previous and isinstance(previous.get("processed_compile_task_ids"), list):
                index["processed_compile_task_ids"] = list(previous["processed_compile_task_ids"])[-100:]
            await asyncio.to_thread(owner._save_knowledge_link_index, selected, index, wiki_root)
            graph = build_wiki_graph(documents, index, wiki_root=wiki_root)
            index_node = next(
                (node for node in graph["nodes"] if node.get("category") == "index"),
                None,
            )
            title = str((index_node or {}).get("title") or "知识库图谱")
            rendered = render_wiki_graph_html(graph, title=title)
            return JSONResponse(
                {
                    "account": selected,
                    "wiki_root": wiki_root,
                    "source": "openviking-api",
                    "renderer": "local-html-fallback",
                    "page_count": len(graph["nodes"]),
                    "edge_count": len(graph["links"]),
                    "generated_at": index["generated_at"],
                    "html": rendered,
                }
            )

        @app.get("/api/knowledge-mining/content")
        async def knowledge_mining_content(
            request: Request,
            account: str = Query(...),
            kind: str = Query(...),
            uri: str = Query(...),
            wiki_root: str = Query(_WIKI_ROOT),
        ):
            """Read one source or Wiki file as account-scoped L2 text."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            selected_wiki_root = _knowledge_wiki_root(wiki_root)
            target = _knowledge_file_uri(kind, uri, wiki_root=selected_wiki_root)
            result = await owner._knowledge_request(
                selected,
                "GET",
                "/api/v1/content/read",
                params={"uri": target, "offset": 0, "limit": -1, "raw": "true"},
            )
            page_links = {"links": [], "backlinks": [], "link_index": None}
            if kind == "wiki" and target.lower().endswith((".md", ".markdown", ".mdx")):
                try:
                    page_links = await owner._knowledge_page_links(
                        selected,
                        target,
                        wiki_root=selected_wiki_root,
                    )
                except Exception as exc:  # Link indexing must not hide an otherwise readable page.
                    logger.warning("Cannot read Wiki backlinks for %s: %s", target, exc)
                    page_links["link_index"] = {"status": "error", "message": str(exc)}
            return JSONResponse(
                {
                    "account": selected,
                    "kind": kind,
                    "uri": target,
                    "name": target.rsplit("/", 1)[-1],
                    "content": _content_text(result),
                    "editable": _content_editable(target),
                    **page_links,
                }
            )

        @app.post("/api/knowledge-mining/content")
        async def knowledge_mining_write(request: Request, body: dict[str, Any]):
            """Replace one editable source or Wiki file with conflict detection."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            kind = str(body.get("kind") or "")
            selected_wiki_root = _knowledge_wiki_root(body.get("wiki_root"))
            target = _knowledge_file_uri(kind, body.get("uri"), wiki_root=selected_wiki_root)
            if not _content_editable(target):
                raise HTTPException(status_code=400, detail="this file type is read-only")
            content = body.get("content")
            original = body.get("original_content")
            if not isinstance(content, str) or not isinstance(original, str):
                raise HTTPException(status_code=400, detail="file content must be text")
            if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
                raise HTTPException(status_code=413, detail="knowledge files cannot exceed 2 MB")

            current_result = await owner._knowledge_request(
                selected,
                "GET",
                "/api/v1/content/read",
                params={"uri": target, "offset": 0, "limit": -1, "raw": "true"},
            )
            current = _content_text(current_result)
            if current not in {original, content}:
                raise HTTPException(status_code=409, detail="文件已被其他操作更新，请重新打开后再编辑")

            result = await owner._knowledge_request(
                selected,
                "POST",
                "/api/v1/content/write",
                # The editor only needs OV to accept the durable replacement.
                # Waiting for the downstream ingestion pipeline is what left
                # the Save button spinning even though the bytes had landed.
                json={"uri": target, "content": content, "mode": "replace", "wait": False},
            )
            link_index = None
            if kind == "wiki" and target.lower().endswith((".md", ".markdown", ".mdx")):
                # Give OV a short window to expose the asynchronously accepted
                # replacement, then rebuild without holding the save response.
                owner._start_knowledge_link_rebuild(
                    selected,
                    wiki_root=selected_wiki_root,
                    delay=0.5,
                )
                link_index = {"status": "pending"}
            return JSONResponse(
                {
                    "saved": True,
                    "account": selected,
                    "kind": kind,
                    "uri": target,
                    "content": content,
                    "result": result,
                    "link_index": link_index,
                }
            )

        @app.post("/api/knowledge-mining/upload", status_code=202)
        async def knowledge_mining_upload(request: Request, body: dict[str, Any]):
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            kind = str(body.get("kind") or "source").strip().lower()
            if kind not in {"source", "wiki"}:
                raise HTTPException(status_code=400, detail="kind must be source or wiki")
            selected_wiki_root = _knowledge_wiki_root(body.get("wiki_root"))
            upload_root = _SOURCE_ROOT if kind == "source" else selected_wiki_root
            upload_parent = _knowledge_directory_uri(
                kind,
                body.get("parent_uri") or upload_root,
                wiki_root=selected_wiki_root,
            )
            files = body.get("files")
            if not isinstance(files, list) or not files:
                raise HTTPException(status_code=400, detail="at least one file is required")
            if len(files) > _MAX_UPLOAD_FILES:
                raise HTTPException(
                    status_code=413,
                    detail=f"at most {_MAX_UPLOAD_FILES} files can be uploaded at once",
                )
            originals: list[tuple[str, bytes]] = []
            original_total_bytes = 0
            seen: set[str] = set()
            for item in files:
                if not isinstance(item, dict):
                    raise HTTPException(status_code=400, detail="invalid upload file")
                relative_path = _safe_relative_path(item.get("relative_path") or item.get("name"))
                if relative_path in seen:
                    raise HTTPException(status_code=400, detail=f"duplicate upload path: {relative_path}")
                seen.add(relative_path)
                encoded = item.get("content_b64")
                if not isinstance(encoded, str) or not encoded:
                    raise HTTPException(status_code=400, detail=f"file content is empty: {relative_path}")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=f"invalid file encoding: {relative_path}") from exc
                if len(raw) > _MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"file exceeds 20 MB: {relative_path}")
                original_total_bytes += len(raw)
                if original_total_bytes > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="upload exceeds 100 MB in total")
                originals.append((relative_path, raw))

            prepared: list[tuple[str, bytes]] = []
            conversions: list[dict[str, Any]] = []
            converted_paths: set[str] = set()
            converted_total_bytes = 0
            for relative_path, raw in originals:
                try:
                    converted = await asyncio.to_thread(
                        convert_knowledge_upload,
                        PurePosixPath(relative_path).name,
                        raw,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                markdown_path = str(PurePosixPath(relative_path).with_suffix(".md"))
                if markdown_path in converted_paths:
                    raise HTTPException(
                        status_code=400,
                        detail=f"multiple files convert to the same Markdown path: {markdown_path}",
                    )
                converted_paths.add(markdown_path)
                markdown_bytes = converted.markdown.encode("utf-8")
                if len(markdown_bytes) > _MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"converted Markdown exceeds 20 MB: {markdown_path}",
                    )
                converted_total_bytes += len(markdown_bytes)
                if converted_total_bytes > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="converted upload exceeds 100 MB in total")
                prepared.append((markdown_path, markdown_bytes))
                conversions.append(
                    {
                        "source_path": relative_path,
                        "markdown_path": markdown_path,
                        "source_format": converted.source_format,
                        "source_encoding": converted.source_encoding,
                        "converted": PurePosixPath(relative_path).suffix.lower() not in {".md", ".markdown"},
                    }
                )

            # Resource import treats a single document as a resource bundle and
            # therefore creates an extra same-named directory.  Knowledge files
            # are ordinary tree entries, so write every converted Markdown file
            # to its exact destination instead.  Folder uploads retain the
            # browser-provided relative path below the selected directory.
            directories = {upload_parent}
            destinations: list[tuple[str, str, bytes]] = []
            for relative_path, raw in prepared:
                target = _knowledge_file_uri(
                    kind,
                    f"{upload_parent}/{relative_path}",
                    wiki_root=selected_wiki_root,
                )
                destinations.append((relative_path, target, raw))
                parent = target.rsplit("/", 1)[0]
                while parent == upload_parent or parent.startswith(f"{upload_parent}/"):
                    directories.add(parent)
                    if parent == upload_parent:
                        break
                    parent = parent.rsplit("/", 1)[0]

            for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
                try:
                    await owner._knowledge_request(
                        selected,
                        "POST",
                        "/api/v1/fs/mkdir",
                        json={"uri": directory},
                    )
                except HTTPException as exc:
                    message = str(exc.detail).upper()
                    if exc.status_code != 409 and "ALREADY_EXISTS" not in message and "CONFLICT" not in message:
                        raise

            written: list[dict[str, Any]] = []
            for relative_path, target, raw in destinations:
                content = raw.decode("utf-8")
                try:
                    result = await owner._knowledge_request(
                        selected,
                        "POST",
                        "/api/v1/content/write",
                        timeout=180.0,
                        json={"uri": target, "content": content, "mode": "create", "wait": False},
                    )
                except HTTPException as exc:
                    message = str(exc.detail).upper()
                    if exc.status_code != 409 and "ALREADY_EXISTS" not in message and "CONFLICT" not in message:
                        raise
                    result = await owner._knowledge_request(
                        selected,
                        "POST",
                        "/api/v1/content/write",
                        timeout=180.0,
                        json={"uri": target, "content": content, "mode": "replace", "wait": False},
                    )
                written.append({"relative_path": relative_path, "uri": target, "result": result})
            link_index = None
            if kind == "wiki":
                owner._start_knowledge_link_rebuild(
                    selected,
                    wiki_root=selected_wiki_root,
                    delay=0.5,
                )
                link_index = {"status": "pending"}
            return JSONResponse(
                status_code=202,
                content={
                    "ok": True,
                    "account": selected,
                    "kind": kind,
                    "root_uri": upload_root,
                    "parent_uri": upload_parent,
                    "file_count": len(prepared),
                    "original_total_bytes": original_total_bytes,
                    "total_bytes": converted_total_bytes,
                    "files": conversions,
                    "written": written,
                    "link_index": link_index,
                },
            )

        @app.delete("/api/knowledge-mining/entry")
        async def knowledge_mining_delete(request: Request, body: dict[str, Any]):
            """Delete one file or directory below a knowledge workspace root."""
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            kind = str(body.get("kind") or "").strip().lower()
            selected_wiki_root = _knowledge_wiki_root(body.get("wiki_root"))
            target = _knowledge_file_uri(kind, body.get("uri"), wiki_root=selected_wiki_root)
            result = await owner._knowledge_request(
                selected,
                "DELETE",
                "/api/v1/fs",
                timeout=180.0,
                params={"uri": target},
            )
            link_index = None
            if kind == "wiki":
                owner._start_knowledge_link_rebuild(
                    selected,
                    wiki_root=selected_wiki_root,
                    delay=0.5,
                )
                link_index = {"status": "pending"}
            return JSONResponse(
                {
                    "deleted": True,
                    "account": selected,
                    "kind": kind,
                    "uri": target,
                    "result": result,
                    "link_index": link_index,
                }
            )

        @app.post("/api/knowledge-mining/tasks", status_code=202)
        async def knowledge_mining_start(request: Request, body: dict[str, Any]):
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            source_uri = str(body.get("source_uri") or _SOURCE_ROOT).strip().rstrip("/")
            target_uri = str(body.get("target_uri") or _WIKI_ROOT).strip().rstrip("/")
            if source_uri != _SOURCE_ROOT or target_uri != _WIKI_ROOT:
                raise HTTPException(
                    status_code=400,
                    detail="knowledge mining uses the configured input and output roots",
                )
            skill_uri = str(body.get("skill_uri") or _DEFAULT_SKILL_URI).strip().rstrip("/")
            if not skill_uri.startswith("viking://agent/skills/"):
                raise HTTPException(status_code=400, detail="skill_uri must be an account-shared Skill")
            instruction = str(body.get("instruction") or "").strip()
            if len(instruction) > 4000:
                raise HTTPException(status_code=400, detail="instruction cannot exceed 4000 characters")

            capabilities = await owner._knowledge_compile_capabilities(selected)
            if not capabilities.get("can_create"):
                reason = str(capabilities.get("reason_code") or "COMPILE_UNAVAILABLE")
                raise HTTPException(status_code=409, detail=f"OpenViking Compile is unavailable: {reason}")

            # The default compiler Skill is provisioned idempotently so a new
            # account can run its first mining task without a separate setup step.
            if skill_uri == _DEFAULT_SKILL_URI:
                client = CompileClient(
                    endpoint=owner._knowledge_endpoint(),
                    account_id=selected,
                    user_id=owner._knowledge_team_user(),
                    api_key=owner._knowledge_api_key(),
                    agent_id="team-skill-evolver",
                )
                provisioned = await client.publish_shared_skill(
                    skill_name="llm-wiki",
                    skill_body=_DEFAULT_WIKI_SKILL,
                    version_message="Provision teamEvolver knowledge Wiki compiler",
                )
                if not provisioned.get("ok"):
                    raise HTTPException(
                        status_code=502,
                        detail=str(provisioned.get("stderr") or "cannot provision the default Wiki compiler Skill"),
                    )

            # The key lets OV return the existing task if the browser retries
            # after a lost response, instead of starting the same Compile twice.
            submission_key = f"te-km-{uuid.uuid4().hex}"
            last_compile_time = await asyncio.to_thread(
                owner._knowledge_last_compile_time,
                selected,
                source_uri,
                target_uri,
                skill_uri,
            )
            compile_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            compile_args = {"last_compile_time": last_compile_time} if last_compile_time else None
            accepted = await owner._knowledge_request(
                selected,
                "POST",
                "/api/v1/compile",
                timeout=60.0,
                extra_headers={"Idempotency-Key": submission_key},
                json={
                    "from": [source_uri],
                    "to": target_uri,
                    "skill": skill_uri,
                    **({"instruction": instruction} if instruction else {}),
                    **({"args": compile_args} if compile_args else {}),
                },
            )
            task_id = str((accepted or {}).get("task_id") or "") if isinstance(accepted, dict) else ""
            if not task_id:
                raise HTTPException(status_code=502, detail="OpenViking compile did not return task_id")
            await asyncio.to_thread(
                owner._record_knowledge_compile_submission,
                selected,
                task_id,
                compile_started_at=compile_started_at,
                source_uri=source_uri,
                target_uri=target_uri,
                skill_uri=skill_uri,
                last_compile_time=last_compile_time,
            )
            owner._start_knowledge_compile_watch(selected, task_id)
            return JSONResponse(
                status_code=202,
                content={
                    "ok": True,
                    "account": selected,
                    "task_id": task_id,
                    "source_uri": source_uri,
                    "target_uri": target_uri,
                    "skill_uri": skill_uri,
                    "compile_started_at": compile_started_at,
                    "last_compile_time": last_compile_time or None,
                    "incremental": bool(last_compile_time),
                    "submission_key": submission_key,
                    "task": accepted,
                },
            )

        @app.get("/api/knowledge-mining/tasks")
        async def knowledge_mining_tasks(request: Request, account: str = Query(...)):
            _require_admin_request(request)
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            payload = await owner._knowledge_request(
                selected,
                "GET",
                "/api/v1/tasks",
                params={"limit": 100},
            )
            rows = payload.get("tasks") or payload.get("items") or [] if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                rows = []
            tasks = []
            for row in rows:
                if not isinstance(row, dict) or str(row.get("task_type") or "").lower() != "compile":
                    continue
                resource_id = str(row.get("resource_id") or row.get("target_uri") or "")
                nested = row.get("result") if isinstance(row.get("result"), dict) else {}
                target = str(nested.get("to") or resource_id)
                if target and target.rstrip("/") != _WIKI_ROOT:
                    continue
                tasks.append({**row, "progress": _knowledge_compile_progress(row)})
            return JSONResponse({"account": selected, "tasks": tasks})

        @app.get("/api/knowledge-mining/tasks/{task_id}")
        async def knowledge_mining_task(request: Request, task_id: str, account: str = Query(...)):
            _require_admin_request(request)
            if not _TASK_ID_RE.fullmatch(task_id):
                raise HTTPException(status_code=400, detail="invalid task id")
            selected = await asyncio.to_thread(owner._knowledge_account, account)
            task = await owner._knowledge_request(
                selected,
                "GET",
                f"/api/v1/tasks/{task_id}",
                params={"include_events": "true"},
            )
            link_index = None
            task_status = str(task.get("status") or "").lower() if isinstance(task, dict) else ""
            if task_status in _COMPILE_TERMINAL_STATES:
                await asyncio.to_thread(owner._finalize_knowledge_compile, selected, task_id, task_status)
            if isinstance(task, dict):
                task = {**task, "progress": _knowledge_compile_progress(task)}
            if task_status in _COMPILE_SUCCESS_STATES:
                try:
                    rebuilt = await owner._knowledge_rebuild_link_index(
                        selected,
                        compile_task_id=task_id,
                    )
                    link_index = owner._knowledge_link_index_summary(rebuilt)
                except Exception as exc:  # Compile status remains observable if post-processing fails.
                    logger.warning("Cannot rebuild Wiki link index after Compile task %s: %s", task_id, exc)
                    link_index = {"status": "error", "message": str(exc)}
            return JSONResponse({"account": selected, "task": task, "link_index": link_index})

        @app.post("/api/knowledge-mining/tasks/{task_id}/cancel")
        async def knowledge_mining_cancel(request: Request, task_id: str, body: dict[str, Any]):
            _require_admin_request(request)
            if not _TASK_ID_RE.fullmatch(task_id):
                raise HTTPException(status_code=400, detail="invalid task id")
            selected = await asyncio.to_thread(owner._knowledge_account, body.get("account"))
            task = await owner._knowledge_request(
                selected,
                "POST",
                f"/api/v1/tasks/{task_id}/cancel",
            )
            return JSONResponse({"account": selected, "task": task})
