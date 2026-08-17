"""Policy enforcement tools for shared memory maintenance."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Set

from .base import Tool, ToolResult
from .viking import (
    VikingHTTPClient,
    _finish_memory_change,
    _prepare_memory_change,
    _sanitize_user_facing_text,
    in_maintained_space,
    in_readable_space,
    maintained_space_root,
)

if TYPE_CHECKING:
    from ..memory_changes import MemoryChangeLedger
    from ..semantic import SemanticMatcher

logger = logging.getLogger(__name__)


class MemoryAuditTool(Tool):
    """Audit shared memory for duplicate-prone and misbranded content."""

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        matcher: "SemanticMatcher | None" = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._matcher = matcher

    @property
    def name(self) -> str:
        return "memory_audit"

    @property
    def description(self) -> str:
        return (
            "Audit the maintained user memory for duplicate-prone files, temporary reports, "
            "and wording that incorrectly presents a maintenance project as the team name. "
            "Returns filename-topic duplicate groups AND content-similarity groups (files whose "
            "bodies overlap heavily even when filenames differ)."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Viking URI to audit recursively (default: your own memories).",
                },
                "limit": {"type": "integer", "description": "Maximum findings to return (default: 50)."},
            },
        }

    def execute(self, **kwargs) -> ToolResult:
        path = kwargs.get("path") or maintained_space_root(self._customer_id)
        if not in_readable_space(path, self._client.user):
            return ToolResult(False, f"DENIED: can only audit your own memories/ or peers/ URIs: {path}")
        limit = int(kwargs.get("limit", 50))
        findings: List[Dict[str, Any]] = []

        try:
            uris = list(self._list_markdown_uris(path))

            # Read each doc once; reuse for both content-similarity and policy scan.
            contents: Dict[str, str] = {uri: self._read(uri) for uri in uris}

            # 1) Filename-topic duplicate groups.
            groups: Dict[str, List[str]] = {}
            for uri in uris:
                name = uri.rsplit("/", 1)[-1]
                key = self._topic_key(name)
                groups.setdefault(key, []).append(uri)

            for key, items in sorted(groups.items()):
                if len(items) > 1 and key not in {"overview", "abstract"}:
                    findings.append({
                        "type": "possible_duplicate_topic",
                        "topic": key,
                        "uris": items,
                        "suggestion": "read candidates, keep one authoritative survivor, archive redundant variants",
                    })
                    if len(findings) >= limit:
                        return ToolResult(True, json.dumps(findings, ensure_ascii=False, indent=2))

            # 2) Content-similarity groups — semantic, catches redundant docs
            #    whose filenames differ but whose meaning overlaps (e.g. several
            #    entries covering the same deliverable). Grouped per directory.
            #    Skipped when no embedding backend is available (never lexical).
            for group in self._semantic_similarity_groups(contents):
                findings.append({
                    "type": "similar_content_group",
                    "uris": group,
                    "suggestion": "read all, merge durable conclusions into one authoritative survivor, archive the rest",
                })
                if len(findings) >= limit:
                    return ToolResult(True, json.dumps(findings, ensure_ascii=False, indent=2))

            bad_name = "Dream" + "Cycle"
            suspicious_terms = [
                bad_name.lower() + "-",
                f"{bad_name} 团队",
                f"{bad_name}团队",
                f"{bad_name} 小组",
                f"{bad_name}小组",
                f"{bad_name} 知识库",
                f"欢迎加入 {bad_name}",
                "搜索友好入口",
            ]
            for uri in uris:
                lower_uri = uri.lower()
                uri_hits = [term for term in suspicious_terms if term.lower() in lower_uri]
                content = contents.get(uri, "")
                content_hits = [term for term in suspicious_terms if term in content] if content else []
                if uri_hits or content_hits:
                    findings.append({
                        "type": "policy_candidate",
                        "uri": uri,
                        "uri_hits": uri_hits,
                        "content_hits": content_hits[:10],
                        "suggestion": "sanitize wording, merge durable facts, or archive if this is a redundant/temporary artifact",
                    })
                    if len(findings) >= limit:
                        break

            return ToolResult(True, json.dumps(findings, ensure_ascii=False, indent=2), metadata={"count": len(findings)})
        except Exception as e:
            return ToolResult(False, "", error=str(e))

    def _list_markdown_uris(self, path: str) -> Iterable[str]:
        resp = self._client.get("/api/v1/fs/ls", params={"uri": path, "recursive": "true", "node_limit": 1000})
        if resp.status_code != 200:
            return []
        data = resp.json().get("result", [])
        uris: List[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri") or entry.get("path") or ""
            name = entry.get("name") or uri.rsplit("/", 1)[-1]
            if uri.endswith(".md") or name.endswith(".md"):
                if not uri.startswith("viking://"):
                    uri = f"{path.rstrip('/')}/{name}"
                uris.append(uri)
        return uris

    def _read(self, uri: str) -> str:
        resp = self._client.get("/api/v1/content/read", params={"uri": uri})
        if resp.status_code != 200:
            return ""
        data = resp.json()
        content = data.get("result", data.get("content", ""))
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def _semantic_similarity_groups(
        self, contents: Dict[str, str], threshold: float = 0.86
    ) -> List[List[str]]:
        """Group same-directory docs whose *meaning* overlaps by embedding.

        Compares only within the same parent directory to avoid cross-topic
        noise. Returns nothing when no embedding backend is configured — the
        audit never falls back to lexical token overlap.
        """
        if self._matcher is None or not getattr(self._matcher, "enabled", False):
            return []

        from ..semantic import _cosine  # local import: optional dependency path

        by_dir: Dict[str, List[str]] = {}
        for uri in contents:
            if contents.get(uri, "").strip():
                by_dir.setdefault(uri.rsplit("/", 1)[0], []).append(uri)

        groups: List[List[str]] = []
        for _dir, uris in by_dir.items():
            if len(uris) < 2:
                continue
            vectors = self._matcher._embed([contents[u] for u in uris])
            if vectors is None:
                continue
            vec_by_uri = dict(zip(uris, vectors))
            visited: Set[str] = set()
            for i, a in enumerate(uris):
                if a in visited:
                    continue
                cluster = [a]
                for b in uris[i + 1:]:
                    if b in visited:
                        continue
                    if _cosine(vec_by_uri[a], vec_by_uri[b]) >= threshold:
                        cluster.append(b)
                        visited.add(b)
                if len(cluster) > 1:
                    visited.update(cluster)
                    groups.append(cluster)
        return groups

    @staticmethod
    def _topic_key(name: str) -> str:
        key = name.rsplit(".", 1)[0].lower()
        for token in [
            ("Dream" + "Cycle").lower() + "-", "（合并版）", "合并版", "搜索友好", "入口", "start_here-", "2026-06-03",
            "2026-05-31", "2026h1", "报告", "诊断", "新人", "团队", "知识库", "导航",
        ]:
            key = key.replace(token, "")
        key = key.replace("—", "-").replace("_", "-").strip("- ")
        return key or name


class MemorySanitizeTool(Tool):
    """Sanitize a shared-memory document in place."""

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        team_name: str = "",
        change_ledger: "MemoryChangeLedger | None" = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._team_name = team_name
        self._change_ledger = change_ledger

    @property
    def name(self) -> str:
        return "memory_sanitize"

    @property
    def description(self) -> str:
        return "Replace maintenance-project-as-team wording with neutral shared-memory wording in one maintained-space memory document."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "viking:// URI to sanitize in place."}},
            "required": ["uri"],
        }

    def execute(self, **kwargs) -> ToolResult:
        uri = kwargs.get("uri", "")
        if not in_maintained_space(uri, self._customer_id, self._client.user):
            return ToolResult(False, f"DENIED: only your own {maintained_space_root(self._customer_id)} URIs are allowed: {uri}")
        try:
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
            if resp.status_code != 200:
                return ToolResult(False, f"FAILED: read HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = data.get("result", data.get("content", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            sanitized, changes = _sanitize_user_facing_text(
                content,
                self._team_name,
            )
            sanitized = sanitized.replace(("Dream" + "Cycle").lower() + "-", "")
            if sanitized == content:
                return ToolResult(True, f"OK: no changes needed for {uri}")
            change = _prepare_memory_change(
                self._change_ledger,
                action="sanitize",
                target_paths=[uri],
                source_refs=[uri],
                reason="remove forbidden maintenance-project wording",
                before_path=uri,
                after_path=uri,
            )
            write = self._client.post("/api/v1/content/write", json={"uri": uri, "content": sanitized, "mode": "replace"})
            if write.status_code == 200:
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="applied",
                    metadata={
                        "sanitized_fields": ",".join(sorted(set(changes))),
                        "write_status": write.status_code,
                    },
                )
                return ToolResult(
                    True,
                    f"OK: sanitized {uri}; changes={sorted(set(changes))}",
                    metadata=metadata,
                )
            error = f"write HTTP {write.status_code}: {write.text[:200]}"
            metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="failed",
                error=error,
                metadata={"write_status": write.status_code},
            )
            return ToolResult(
                False,
                f"FAILED: {error}",
                metadata=metadata,
            )
        except Exception as e:
            if "change" in locals():
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="failed",
                    error=str(e),
                )
            else:
                metadata = {}
            return ToolResult(False, "", metadata=metadata, error=str(e))
