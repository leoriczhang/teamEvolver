"""OpenViking tools for the memory maintenance agent.

Uses the REAL OpenViking filesystem API:
- Search:  POST /api/v1/search/find
- Read:    GET  /api/v1/content/read
- Write:   POST /api/v1/content/write
- List:    GET  /api/v1/fs/ls
- Delete:  DELETE /api/v1/fs
- Move:    POST /api/v1/fs/mv

Scope — DreamCycle maintains USER MEMORY only:
- READ tools (search, read, browse): can access all users' content
- WRITE tools (remember, forget): restricted to the authenticated user's own
  memory subtree ``viking://user/memories/`` (or a single peer subtree
  ``viking://user/peers/{customer_id}/memories/``). It never touches
  resources/skills/sessions or any non-memory namespace.

The maintained-space URI uses the server's user-relative shorthand
(``viking://user/...``), which OpenViking normalizes to the *authenticated*
user's own subtree. This binds every operation to whoever the request
authenticates as, instead of a hardcoded namespace that may not be owned.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx

from ..config import OpenVikingConfig, parse_openviking_key
from ..policy import team_reference
from .base import Tool, ToolResult

if TYPE_CHECKING:
    from ..blackboard import Blackboard
    from ..memory_changes import MemoryChangeLedger, PreparedMemoryChange
    from ..semantic import SemanticMatcher

logger = logging.getLogger(__name__)


_MAINTENANCE_PROJECT_NAME = "Dream" + "Cycle"


def _prepare_memory_change(
    ledger: "MemoryChangeLedger | None",
    **kwargs: Any,
) -> "PreparedMemoryChange | None":
    if ledger is None:
        return None
    try:
        return ledger.prepare(**kwargs)
    except Exception:
        logger.exception("[DreamCycle] failed to prepare Memory Change")
        return None


def _finish_memory_change(
    ledger: "MemoryChangeLedger | None",
    token: "PreparedMemoryChange | None",
    *,
    result: str,
    error: str = "",
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if ledger is None or token is None:
        return {}
    try:
        return {
            "memory_change": ledger.finish(
                token,
                result=result,
                error=error,
                metadata=metadata,
            )
        }
    except Exception:
        logger.exception("[DreamCycle] failed to finish Memory Change")
        return {}


_USER_RELATIVE_ROOTS = frozenset(
    {"memories", "resources", "skills", "peers", "privacy", "sessions"}
)


def maintained_space_root(customer_id: str = "") -> str:
    """Return the maintained user-memory root URI (with trailing slash), as a request URI.

    DreamCycle maintains USER MEMORY only. Requests use OpenViking's
    user-relative shorthand (``viking://user/...``), which the server
    normalizes to the *authenticated* user's own subtree — so operations bind
    to whoever the request authenticates as, never to a hardcoded namespace
    the caller may not own.

    - Without ``customer_id``: the authenticated user's own memories,
      ``viking://user/memories/``.
    - With ``customer_id``: a single peer's memories,
      ``viking://user/peers/{customer_id}/memories/``.
    """
    if customer_id:
        return f"viking://user/peers/{customer_id}/memories/"
    return "viking://user/memories/"


def _uri_segments(uri: str) -> List[str]:
    stripped = uri.split("?", 1)[0]
    if stripped.startswith("viking://"):
        stripped = stripped[len("viking://"):]
    return [part for part in stripped.strip("/").split("/") if part]


def _user_relative_segments(uri: str, owner: str = "") -> Optional[List[str]]:
    """Return path segments after the user root (memory-space relative).

    Handles both the shorthand form ``viking://user/memories/...`` and the
    canonical form the server returns, ``viking://user/{owner}/memories/...``.
    Returns ``None`` when the URI is not under the ``user`` scope, or when it
    is a canonical URI owned by a *different* user than ``owner``.
    """
    parts = _uri_segments(uri)
    if not parts or parts[0] != "user":
        return None
    rest = parts[1:]
    if not rest:
        return []
    if rest[0] in _USER_RELATIVE_ROOTS:
        # Shorthand: resolves to the authenticated user's own subtree.
        return rest
    # Canonical: rest[0] is the owner id. Reject other users' spaces.
    if owner and rest[0] != owner:
        return None
    return rest[1:]


def in_maintained_space(uri: str, customer_id: str = "", owner: str = "") -> bool:
    """Return True when ``uri`` is an own-user-memory URI within the maintained scope.

    Structural, memory-scoped check that accepts a memory URI in either
    shorthand or canonical form and rejects any non-memory content
    (resources/skills/sessions/etc.), enforcing that DreamCycle only ever
    touches user memory. Canonical URIs owned by a different user than
    ``owner`` are rejected. When ``customer_id`` is set, only that peer's
    memories are in scope; otherwise the user's own top-level memories are in
    scope (peer memories excluded).
    """
    rel = _user_relative_segments(uri, owner)
    if rel is None:
        return False
    expected = ["peers", customer_id, "memories"] if customer_id else ["memories"]
    return rel[: len(expected)] == expected


def _archived_uri(uri: str) -> str:
    """Return the archive path for a memory URI by inserting ``_archived/``
    right after the ``memories/`` segment (works for shorthand and canonical)."""
    return uri.replace("/memories/", "/memories/_archived/", 1)


_READABLE_USER_ROOTS = frozenset({"memories", "peers"})


def in_readable_space(uri: str, owner: str = "") -> bool:
    """Return True when ``uri`` is inside the authenticated user's own readable scope.

    Reads are confined to the user's OWN memory namespaces: their own
    ``memories/`` subtree and their ``peers/`` subtree (per-peer memories).
    Rejects any other user's space and any non-memory namespace
    (resources/skills/sessions/privacy/etc.). Accepts both the user-relative
    shorthand and the canonical form the server returns.
    """
    rel = _user_relative_segments(uri, owner)
    if not rel:  # None (foreign/non-user) or [] (bare user root — too broad)
        return False
    return rel[0] in _READABLE_USER_ROOTS


def user_root() -> str:
    """The authenticated user's own root URI (user-relative shorthand)."""
    return "viking://user/"


def _canonical_user_uri(uri: str, user: str) -> str:
    """Attach the authenticated source user to a relative user URI."""
    parts = _uri_segments(uri)
    if (
        user
        and len(parts) > 1
        and parts[0] == "user"
        and parts[1] in _USER_RELATIVE_ROOTS
    ):
        return f"viking://user/{user}/{'/'.join(parts[1:])}"
    return uri


def _search_result_items(payload: Any) -> List[Any]:
    """Normalize OpenViking's flat and categorized search envelopes."""
    result = payload.get("result", payload.get("items", []))
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("items"), list):
        return result["items"]
    items: List[Any] = []
    for category in ("memories", "resources", "skills"):
        values = result.get(category)
        if isinstance(values, list):
            items.extend(values)
    return items


FORBIDDEN_USER_FACING_TERMS = (
    f"{_MAINTENANCE_PROJECT_NAME} 团队",
    f"{_MAINTENANCE_PROJECT_NAME}团队",
    f"{_MAINTENANCE_PROJECT_NAME} 小组",
    f"{_MAINTENANCE_PROJECT_NAME}小组",
    f"{_MAINTENANCE_PROJECT_NAME} 知识库",
    f"欢迎加入 {_MAINTENANCE_PROJECT_NAME}",
)


def _sanitize_user_facing_text(
    text: str,
    team_name: str = "",
) -> Tuple[str, List[str]]:
    """Remove maintenance-project wording from user-facing shared memory text.

    The maintenance project name must never surface as a team/knowledge-base
    brand. We strip that wording rather than substituting a specific team name,
    so the maintainer stays team-agnostic.
    """
    name = _MAINTENANCE_PROJECT_NAME
    lower_name = name.lower()
    team = team_reference(team_name)
    replacements = {
        f"{name} 团队知识库": f"{team}共享知识库",
        f"{name}团队知识库": f"{team}共享知识库",
        f"{name} 知识库": f"{team}知识库",
        f"{name} 团队": team,
        f"{name}团队": team,
        f"{name} 小组": team,
        f"{name}小组": team,
        f"欢迎加入 {name}！": f"欢迎加入 {team}！",
        f"欢迎加入 {name} !": f"欢迎加入 {team}!",
        f"欢迎加入 {name}!": f"欢迎加入 {team}!",
        f"欢迎加入 {name}": f"欢迎加入 {team}",
        f"{name} 新人": f"{team}新人",
        name: "维护流程",
        lower_name: "memory-maintainer",
    }
    changed: List[str] = []
    sanitized = text
    for old, new in replacements.items():
        if old in sanitized:
            sanitized = sanitized.replace(old, new)
            changed.append(old)
    return sanitized, changed




def _looks_like_temporary_artifact(title: str, content: str, category: str) -> Optional[str]:
    """Return a reason if this write should be a report, not long-term memory."""
    title_text = title or ""
    sample = f"{title_text}\n{content[:500]}"
    blocked_title_terms = ("搜索友好", "检查报告", "诊断报告", "搜索功能诊断", "新人友好检查")
    if any(term in title_text for term in blocked_title_terms):
        return "temporary/report/search-workaround content should be saved with save_report, not long-term memory"
    if category == "event" and any(term in sample for term in ("检查执行", "检查结果", "搜索索引", "viking_search 对共享空间")):
        return "maintenance diagnostics belong in local reports unless they are a durable incident summary"
    return None



def _extract_markdown_title(content: str) -> str:
    """Return the first H1 title from markdown content, if any."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()
    return ""


def _strip_leading_front_matter(content: str) -> str:
    """Remove one or more leading YAML front-matter blocks before rewriting."""
    text = content.lstrip()
    while text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end == -1:
            break
        close_end = end + len("\n---")
        if close_end < len(text) and text[close_end:close_end + 1] not in {"", "\n", "\r"}:
            break
        text = text[close_end:].lstrip("\r\n")
    return text


def _comparable_text(title: str, content: str) -> str:
    """Text used for semantic comparison: title plus a body excerpt."""
    return f"{title}\n{content}".strip()


def _slugify_title(title: str, team_name: str = "") -> str:
    """Create a stable, user-facing filename slug without maintenance branding."""
    sanitized, _ = _sanitize_user_facing_text(title.strip(), team_name)
    sanitized = re.sub(r"(?i)^" + re.escape(_MAINTENANCE_PROJECT_NAME) + r"[-_\s]+", "", sanitized)
    for token in ("（合并版）", "合并版", "搜索友好版", "搜索友好", "入口版"):
        sanitized = sanitized.replace(token, "")
    sanitized = sanitized.lower().replace(" ", "-").replace("/", "-")
    sanitized = re.sub(r"[-_]{2,}", "-", sanitized).strip("-_")
    return sanitized[:50] or uuid.uuid4().hex[:12]


class VikingHTTPClient:
    """Shared HTTP client for OpenViking operations."""

    def __init__(
        self,
        config: OpenVikingConfig,
        *,
        user_override: Optional[str] = None,
        api_key_override: Optional[str] = None,
    ):
        self._endpoint = config.endpoint.rstrip("/")
        api_key = (
            str(api_key_override).strip()
            if api_key_override is not None
            else str(config.api_key or "").strip()
        )
        key_account, key_user = parse_openviking_key(api_key)
        self._user = user_override or key_user or config.agent_id
        self._headers = {
            "X-OpenViking-Account": key_account or config.account,
            "X-OpenViking-User": self._user,
        }
        if config.agent:
            self._headers["X-OpenViking-Agent"] = config.agent
        if api_key:
            self._headers["X-API-Key"] = api_key
        self._client = httpx.Client(headers=self._headers, timeout=30.0)

    @property
    def user(self) -> str:
        """The authenticated OpenViking user this client acts as."""
        return self._user

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._client.get(f"{self._endpoint}{path}", **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._client.post(f"{self._endpoint}{path}", **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self._client.delete(f"{self._endpoint}{path}", **kwargs)


class VikingSearchTool(Tool):
    """Search the OpenViking knowledge base via POST /api/v1/search/find."""

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        source_clients: Optional[List[VikingHTTPClient]] = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._source_clients = list(source_clients or [])

    @property
    def name(self) -> str:
        return "viking_search"

    @property
    def description(self) -> str:
        return (
            "Semantic search over your own OpenViking memory. "
            "scope: memories=your own memories/ only (default), "
            "own=your memories/ plus your peers/ memories, "
            "all=team target plus every configured personal-key source."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "scope": {
                    "type": "string",
                    "enum": ["memories", "own", "all"],
                    "description": "memories=team target memories, own=target memories + peers, all=target plus personal-key sources.",
                },
                "limit": {"type": "integer", "description": "Max results (default: 10)."},
            },
            "required": ["query"],
        }

    def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        scope = kwargs.get("scope", "memories")
        limit = kwargs.get("limit", 10)

        if scope == "own":
            target_uri = user_root() if not self._customer_id else maintained_space_root(self._customer_id)
        else:
            target_uri = maintained_space_root(self._customer_id)

        targets = [(self._client, target_uri)]
        if scope == "all":
            targets.extend(
                (client, maintained_space_root())
                for client in self._source_clients
                if client.user
            )
        results: List[Any] = []
        errors: List[str] = []
        total_limit = max(1, int(limit))
        per_target_limit = max(1, total_limit // len(targets))
        for client, uri in targets:
            try:
                resp = client.post(
                    "/api/v1/search/find",
                    json={
                        "query": query,
                        "limit": per_target_limit,
                        "target_uri": uri,
                    },
                )
                if resp.status_code != 200:
                    errors.append(f"{client.user}: HTTP {resp.status_code}")
                    continue
                data = resp.json()
                items = _search_result_items(data)
                items = self._filter_readable(items, client)
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        item = dict(item)
                        item["source_user"] = client.user
                        for field in ("uri", "path"):
                            if item.get(field):
                                item[field] = _canonical_user_uri(
                                    str(item[field]),
                                    client.user,
                                )
                    results.append(item)
            except Exception as exc:
                errors.append(f"{client.user}: {exc}")
        output = json.dumps(results[:total_limit], ensure_ascii=False, indent=2)
        if results or not errors:
            return ToolResult(
                success=True,
                output=output,
                metadata={"count": min(len(results), total_limit), "errors": errors},
            )
        return ToolResult(success=False, output="", error="; ".join(errors))

    def _filter_readable(
        self,
        results: Any,
        client: Optional[VikingHTTPClient] = None,
    ) -> Any:
        """Drop any result outside the user's own memories/ + peers/ scope.

        Defense in depth: even if the backend widens the target, the maintainer
        must never surface other users' data or non-memory namespaces.
        """
        if not isinstance(results, list):
            return results
        owner = (client or self._client).user
        kept = []
        for item in results:
            uri = ""
            if isinstance(item, dict):
                uri = item.get("uri") or item.get("path") or ""
            if uri and not in_readable_space(uri, owner):
                continue
            kept.append(item)
        return kept


class VikingReadTool(Tool):
    """Read content at a viking:// URI via GET /api/v1/content/read."""

    def __init__(
        self,
        client: VikingHTTPClient,
        source_clients: Optional[List[VikingHTTPClient]] = None,
    ):
        self._client = client
        self._clients_by_user = {
            source.user: source for source in (source_clients or []) if source.user
        }

    def _client_for_uri(self, uri: str) -> VikingHTTPClient:
        """Pick the client that owns ``uri`` (a peer source, else the maintainer)."""
        parts = _uri_segments(str(uri or ""))
        if (
            len(parts) > 2
            and parts[0] == "user"
            and parts[1] not in _USER_RELATIVE_ROOTS
        ):
            return self._clients_by_user.get(parts[1], self._client)
        return self._client

    def _read_one(self, uri: str) -> ToolResult:
        """Read a single URI with the correct owning client and scope guard."""
        client = self._client_for_uri(uri)
        if not in_readable_space(uri, client.user):
            return ToolResult(
                success=False,
                output=f"DENIED: can only read your own memories/ or peers/ URIs: {uri}",
            )
        try:
            resp = client.get("/api/v1/content/read", params={"uri": uri})
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("result", data.get("content", ""))
                return ToolResult(success=True, output=content if content else "(empty)")
            elif resp.status_code == 404:
                return ToolResult(success=False, output=f"Not found: {uri}")
            else:
                return ToolResult(success=False, output=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    @property
    def name(self) -> str:
        return "viking_read"

    @property
    def description(self) -> str:
        return "Read content at a viking:// URI within your own memories/ or peers/ space."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "viking:// URI to read."},
            },
            "required": ["uri"],
        }

    def execute(self, **kwargs) -> ToolResult:
        return self._read_one(kwargs.get("uri", ""))


class VikingReadManyTool(Tool):
    """Read several viking:// URIs in one call (dedup/consolidate/merge prep).

    Dedup, consolidation, and merges all need to read a *group* of documents
    before acting. Doing that one ``viking_read`` at a time burns the turn
    budget on IO; this batches them and returns each document keyed by URI.
    """

    def __init__(
        self,
        client: VikingHTTPClient,
        source_clients: Optional[List[VikingHTTPClient]] = None,
    ):
        # Reuse VikingReadTool's per-URI routing + scope guard.
        self._reader = VikingReadTool(client, source_clients=list(source_clients or []))

    @property
    def name(self) -> str:
        return "viking_read_many"

    @property
    def description(self) -> str:
        return (
            "Read multiple viking:// URIs at once (max 20) within your own memories/ "
            "or peers/ space. Use this before dedup/merge to load all candidates in one step."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "viking:// URIs to read (max 20).",
                },
            },
            "required": ["uris"],
        }

    def execute(self, **kwargs) -> ToolResult:
        uris = kwargs.get("uris") or []
        if not isinstance(uris, list) or not uris:
            return ToolResult(success=False, output="uris must be a non-empty list")
        uris = [str(u) for u in uris][:20]
        documents: Dict[str, Any] = {}
        ok = 0
        for uri in uris:
            res = self._reader._read_one(uri)
            documents[uri] = {
                "success": res.success,
                "content": res.output if res.success else "",
                "error": None if res.success else res.output,
            }
            if res.success:
                ok += 1
        # Succeeds as long as at least one document was read; per-URI status is
        # in the payload so the LLM can react to partial failures.
        return ToolResult(
            success=ok > 0,
            output=json.dumps(documents, ensure_ascii=False, indent=2),
            metadata={"requested": len(uris), "read": ok},
        )


class VikingBrowseTool(Tool):
    """Browse the knowledge store directory structure via GET /api/v1/fs/ls."""

    def __init__(self, client: VikingHTTPClient, customer_id: str = ""):
        self._client = client
        self._customer_id = customer_id

    @property
    def name(self) -> str:
        return "viking_browse"

    @property
    def description(self) -> str:
        return (
            "Browse your own memory directory structure (memories/ or peers/). "
            "action: list (contents), tree (recursive), stat (metadata)."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "tree", "stat"]},
                "path": {"type": "string", "description": "Viking URI under your own memories/ or peers/ (default: your own memory root)."},
            },
            "required": ["action"],
        }

    def execute(self, **kwargs) -> ToolResult:
        action = kwargs.get("action", "list")
        path = kwargs.get("path", maintained_space_root(self._customer_id))
        if not in_readable_space(path, self._client.user):
            return ToolResult(
                success=False,
                output=f"DENIED: can only browse your own memories/ or peers/ URIs: {path}",
            )

        try:
            if action == "stat":
                resp = self._client.get("/api/v1/fs/stat", params={"uri": path})
            elif action == "tree":
                resp = self._client.get("/api/v1/fs/ls", params={"uri": path, "recursive": "true", "node_limit": 500})
            else:
                resp = self._client.get("/api/v1/fs/ls", params={"uri": path, "node_limit": 200})

            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", [])
                output = json.dumps(result, ensure_ascii=False, indent=2)
                return ToolResult(success=True, output=output)
            elif resp.status_code == 404:
                return ToolResult(success=True, output="(empty directory or not found)")
            else:
                return ToolResult(success=False, output=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class VikingRememberTool(Tool):
    """Store information in the maintained user-memory space via POST /api/v1/content/write.

    Writes to: viking://user/memories/{category}/{id}.md
    (or the per-peer subtree when customer_id is set). Bound to the
    authenticated user's own memory only.
    """

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        matcher: Optional["SemanticMatcher"] = None,
        team_name: str = "",
        change_ledger: "MemoryChangeLedger | None" = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._matcher = matcher
        self._team_name = team_name
        self._change_ledger = change_ledger

    @property
    def name(self) -> str:
        return "viking_remember"

    @property
    def description(self) -> str:
        return (
            "Store or replace information in your own user-memory space "
            "(viking://user/memories/...). "
            "Default to updating an existing document via target_uri. "
            "Creating a new file requires allow_create_reason and must be a new category or distinct long-term content. "
            "Refer to the group as the team; do not write a maintenance project name into memory."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Information to store (markdown format)."},
                "category": {
                    "type": "string",
                    "description": "Memory category/path segment (default: pattern). Prefer existing categories; new categories require allow_create_reason.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the memory file (used in filename). If omitted, auto-generated. Do not prefix with maintenance project names.",
                },
                "replaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of older/redundant URIs that this memory consolidates.",
                },
                "target_uri": {
                    "type": "string",
                    "description": "Existing viking:// URI to update in place. Strongly preferred over creating a new file.",
                },
                "allow_create_reason": {
                    "type": "string",
                    "description": "Required only for new files: explain why no existing document can carry this distinct long-term team content.",
                },
            },
            "required": ["content"],
        }

    def _load_existing_category_docs(self, category: str) -> Dict[str, str]:
        """Load existing docs in a category for create-gating heuristics."""
        base_uri = f"{maintained_space_root(self._customer_id)}{category}/"
        docs: Dict[str, str] = {}
        try:
            resp = self._client.get("/api/v1/fs/ls", params={"uri": base_uri, "node_limit": 200})
            if resp.status_code != 200:
                return docs
            entries = resp.json().get("result", [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                uri = entry.get("uri") or ""
                name = entry.get("name") or uri.rsplit("/", 1)[-1]
                if not name.endswith(".md") or name.startswith("."):
                    continue
                if not uri.startswith("viking://"):
                    uri = f"{base_uri}{name}"
                read = self._client.get("/api/v1/content/read", params={"uri": uri})
                if read.status_code != 200:
                    continue
                data = read.json()
                doc = data.get("result", data.get("content", ""))
                if not isinstance(doc, str):
                    doc = json.dumps(doc, ensure_ascii=False)
                docs[uri] = f"{_extract_markdown_title(doc)}\n{doc[:2000]}"
        except Exception as e:
            logger.warning("Failed to load existing docs for %s: %s", category, e)
        return docs

    def execute(self, **kwargs) -> ToolResult:
        content = kwargs.get("content", "")
        category = kwargs.get("category", "pattern")
        title = kwargs.get("title", "")
        replaces = kwargs.get("replaces", []) or []
        if not isinstance(replaces, list):
            replaces = [str(replaces)]
        target_uri = kwargs.get("target_uri", "")
        allow_create_reason = kwargs.get("allow_create_reason", "").strip()

        content, content_changes = _sanitize_user_facing_text(
            content,
            self._team_name,
        )
        content = _strip_leading_front_matter(content)
        title, title_changes = _sanitize_user_facing_text(
            title,
            self._team_name,
        )
        blocked_reason = _looks_like_temporary_artifact(title, content, category)
        if blocked_reason:
            return ToolResult(
                success=False,
                output=(
                    "DENIED: this looks like redundant or temporary maintenance output; "
                    f"{blocked_reason}. Merge durable facts into an existing authority or use save_report."
                ),
            )

        existing = self._load_existing_category_docs(category)
        dup_note = ""
        if target_uri:
            if not in_maintained_space(target_uri, self._customer_id, self._client.user):
                return ToolResult(False, f"DENIED: target_uri must be in your own user-memory space {maintained_space_root(self._customer_id)}: {target_uri}")
            uri = target_uri
            mode = "replace"
        else:
            # New files are exceptional. Prefer updating existing docs in the same category.
            if not allow_create_reason:
                candidates = list(existing)[:8]
                return ToolResult(
                    success=False,
                    output=(
                        "DENIED: creating a new memory requires allow_create_reason. "
                        "Default to updating an existing document via target_uri; only create for a new category "
                        "or distinct long-term team content. "
                        f"Candidates: {candidates}"
                    ),
                )
            if existing and self._matcher is not None:
                assessment = self._matcher.assess(
                    _comparable_text(title, content), existing
                )
                verdict = assessment["verdict"]
                best = assessment["best_uri"]
                score = assessment["score"]
                if verdict == "merge":
                    # Semantic duplicate: block creation and hand back the exact
                    # neighbour so the LLM updates it instead of forking a copy.
                    return ToolResult(
                        success=False,
                        output=(
                            "DENIED: this is semantically near-duplicate of an existing "
                            f"document (cosine={score}). Update it in place instead of "
                            "creating a parallel copy:\n"
                            f"  target_uri: {best}\n"
                            "Read it first, merge only the durable new facts, then rewrite "
                            f"via target_uri. Other candidates: {list(existing)[:8]}"
                        ),
                    )
                if verdict == "warn":
                    # Related but plausibly distinct: allow, but surface the neighbour.
                    dup_note = (
                        f"; note: semantically related to {best} (cosine={score}) — "
                        "merge into it if this duplicates it"
                    )
                elif verdict == "unknown":
                    # No embedding backend / call failed: never fall back to lexical
                    # overlap. Allow creation but ask the model to verify manually.
                    dup_note = (
                        "; note: semantic dedup unavailable — verify this is not a "
                        f"duplicate of existing category docs before relying on it: {list(existing)[:8]}"
                    )
            # Generate stable filename. Avoid creating user-facing docs with maintenance-project prefixes.
            slug = (
                _slugify_title(title, self._team_name)
                if title
                else uuid.uuid4().hex[:12]
            )
            uri = f"{maintained_space_root(self._customer_id)}{category}/{slug}.md"
            mode = "replace"

        now = datetime.now(timezone.utc)

        # Add front-matter
        full_content = (
            f"---\n"
            f"category: {category}\n"
            f"created_by: memory-maintainer\n"
            f"created_at: {now.isoformat()}\n"
            f"replaces: {json.dumps(replaces, ensure_ascii=False)}\n"
            f"---\n\n"
            f"{content}"
        )

        payload = {
            "uri": uri,
            "content": full_content,
            "mode": mode,
        }
        change = _prepare_memory_change(
            self._change_ledger,
            action="update" if target_uri else "create",
            target_paths=[uri],
            source_refs=replaces,
            reason=(
                allow_create_reason
                if not target_uri
                else "update existing authoritative memory"
            ),
            before_path=uri,
            after_path=uri,
        )

        try:
            resp = self._client.post("/api/v1/content/write", json=payload)
            if resp.status_code == 200:
                note = ""
                changes = title_changes + content_changes
                if changes:
                    note = f"; sanitized forbidden user-facing terms: {sorted(set(changes))}"
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="applied",
                    metadata={"write_status": resp.status_code},
                )
                return ToolResult(
                    success=True,
                    output=f"OK: stored at {uri}{note}{dup_note}",
                    metadata=metadata,
                )
            elif resp.status_code == 404:
                # Parent directory doesn't exist — try create mode
                payload["mode"] = "create"
                resp2 = self._client.post("/api/v1/content/write", json=payload)
                if resp2.status_code in (200, 201):
                    note = ""
                    changes = title_changes + content_changes
                    if changes:
                        note = f"; sanitized forbidden user-facing terms: {sorted(set(changes))}"
                    metadata = _finish_memory_change(
                        self._change_ledger,
                        change,
                        result="applied",
                        metadata={"write_status": resp2.status_code},
                    )
                    return ToolResult(
                        success=True,
                        output=f"OK: created at {uri}{note}{dup_note}",
                        metadata=metadata,
                    )
                error = f"HTTP {resp2.status_code}: {resp2.text[:200]}"
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="failed",
                    error=error,
                    metadata={"write_status": resp2.status_code},
                )
                return ToolResult(
                    success=False,
                    output=f"FAILED: {error}",
                    metadata=metadata,
                )
            else:
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="failed",
                    error=error,
                    metadata={"write_status": resp.status_code},
                )
                return ToolResult(
                    success=False,
                    output=f"FAILED: {error}",
                    metadata=metadata,
                )
        except Exception as e:
            metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="failed",
                error=str(e),
            )
            return ToolResult(
                success=False,
                output="",
                metadata=metadata,
                error=str(e),
            )


class VikingForgetTool(Tool):
    """Archive memories by moving to _archived/ directory.

    Uses POST /api/v1/fs/mv to move files to archive path.
    Only operates on the authenticated user's own memory space.
    """

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        blackboard: "Blackboard | None" = None,
        change_ledger: "MemoryChangeLedger | None" = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._blackboard = blackboard
        self._change_ledger = change_ledger

    @property
    def name(self) -> str:
        return "viking_forget"

    @property
    def description(self) -> str:
        return (
            "Archive outdated/redundant memories by moving to _archived/ directory. "
            "Only operates on your own memory space URIs. Does NOT delete."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "viking:// URI of memory to archive."},
                "reason": {"type": "string", "description": "Why this is being archived."},
            },
            "required": ["uri"],
        }

    def execute(self, **kwargs) -> ToolResult:
        uri = kwargs.get("uri", "")
        reason = kwargs.get("reason", "memory maintenance")

        # Permission guard: only allow the maintained user-memory space
        if not in_maintained_space(uri, self._customer_id, self._client.user):
            return ToolResult(
                success=False,
                output=f"DENIED: Cannot archive {uri} — only your own {maintained_space_root(self._customer_id)} URIs are allowed.",
            )

        # Move to _archived/ path
        # e.g., viking://user/memories/pattern/foo.md
        #     → viking://user/memories/_archived/pattern/foo.md
        archived_uri = _archived_uri(uri)
        change = _prepare_memory_change(
            self._change_ledger,
            action="archive",
            target_paths=[uri, archived_uri],
            source_refs=[uri],
            reason=reason,
            before_path=uri,
            after_path=archived_uri,
        )

        try:
            resp = self._client.post("/api/v1/fs/mv", json={
                "from_uri": uri,
                "to_uri": archived_uri,
            })
            if resp.status_code == 200:
                if self._blackboard is not None:
                    self._blackboard.mark_processed(uri, "archived", reason)
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="applied",
                )
                return ToolResult(
                    success=True,
                    output=f"OK: archived {uri} → {archived_uri} (reason: {reason})",
                    metadata=metadata,
                )
            elif resp.status_code == 404:
                # File doesn't exist — already gone
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="noop",
                )
                return ToolResult(
                    success=True,
                    output=f"Already gone: {uri}",
                    metadata=metadata,
                )
            else:
                error = f"archive mv HTTP {resp.status_code}: {resp.text[:200]}"
                metadata = _finish_memory_change(
                    self._change_ledger,
                    change,
                    result="failed",
                    error=error,
                )
                return ToolResult(
                    success=False,
                    output=f"FAILED: {error}",
                    metadata=metadata,
                )
        except Exception as e:
            metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="failed",
                error=str(e),
            )
            return ToolResult(
                success=False,
                output="",
                metadata=metadata,
                error=str(e),
            )


class ListCustomersTool(Tool):
    """List all peers under the authenticated user's own peers/ directory."""

    def __init__(
        self,
        client: VikingHTTPClient,
        source_clients: Optional[List[VikingHTTPClient]] = None,
    ):
        self._client = client
        self._source_users = sorted(
            {source.user for source in (source_clients or []) if source.user}
        )

    @property
    def name(self) -> str:
        return "list_customers"

    @property
    def description(self) -> str:
        return "List all peers under your own OpenViking space."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        if self._source_users:
            return ToolResult(
                success=True,
                output=json.dumps(self._source_users, ensure_ascii=False),
                metadata={"count": len(self._source_users)},
            )
        peers_uri = "viking://user/peers/"
        try:
            resp = self._client.get("/api/v1/fs/ls", params={"uri": peers_uri, "node_limit": 100})
            if resp.status_code == 200:
                data = resp.json()
                entries = data.get("result", [])
                customers = []
                for entry in entries:
                    if isinstance(entry, dict):
                        name = entry.get("name", "")
                        uri = entry.get("uri", "")
                        if not name and uri:
                            name = uri.rstrip("/").split("/")[-1]
                        if name and not name.startswith("_"):
                            customers.append(name)
                output = json.dumps(customers, ensure_ascii=False)
                return ToolResult(success=True, output=output, metadata={"count": len(customers)})
            elif resp.status_code == 404:
                return ToolResult(success=True, output="[]", metadata={"count": 0})
            else:
                return ToolResult(success=False, output=f"HTTP {resp.status_code}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class VikingMergeTool(Tool):
    """Atomically consolidate duplicates: write the survivor, then archive sources.

    Merging used to be an unguarded four-step dance (read A, read B, rewrite A,
    archive B) that could leave a half-merged survivor plus lingering copies if
    any step failed. This tool makes it one action with a safe ordering:

    1. Validate the survivor and every source is in the maintained space.
    2. Write the merged ``content`` to ``target_uri`` (replace).  If this fails,
       nothing is archived — the store is left untouched.
    3. Only after the survivor is persisted, archive each source to ``_archived/``.

    Sources equal to the target are skipped so the survivor is never archived.
    """

    def __init__(
        self,
        client: VikingHTTPClient,
        customer_id: str = "",
        blackboard: "Blackboard | None" = None,
        team_name: str = "",
        change_ledger: "MemoryChangeLedger | None" = None,
    ):
        self._client = client
        self._customer_id = customer_id
        self._blackboard = blackboard
        self._team_name = team_name
        self._change_ledger = change_ledger

    @property
    def name(self) -> str:
        return "viking_merge"

    @property
    def description(self) -> str:
        return (
            "Atomically merge duplicates: write the consolidated markdown to target_uri "
            "(an existing survivor), then archive the source URIs. Use this instead of a "
            "manual remember+forget sequence when combining duplicate memories. "
            "All URIs must be in your own memory space."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_uri": {
                    "type": "string",
                    "description": "Survivor URI to overwrite with the merged content (must already exist in your memory space).",
                },
                "content": {
                    "type": "string",
                    "description": "Full merged markdown body for the survivor.",
                },
                "source_uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Duplicate URIs to archive after the survivor is written (max 20).",
                },
                "reason": {"type": "string", "description": "Why these are being merged."},
            },
            "required": ["target_uri", "content", "source_uris"],
        }

    def execute(self, **kwargs) -> ToolResult:
        target_uri = str(kwargs.get("target_uri", "")).strip()
        content = kwargs.get("content", "")
        sources = kwargs.get("source_uris") or []
        reason = kwargs.get("reason", "memory maintenance merge")

        if not target_uri or not isinstance(sources, list) or not sources:
            return ToolResult(success=False, output="target_uri and a non-empty source_uris list are required")
        if not in_maintained_space(target_uri, self._customer_id, self._client.user):
            return ToolResult(
                success=False,
                output=f"DENIED: target_uri must be in your own memory space {maintained_space_root(self._customer_id)}: {target_uri}",
            )

        # Validate every source up front so we never write the survivor and then
        # discover a source we are not allowed to touch.
        sources = [str(s).strip() for s in sources][:20]
        for src in sources:
            if not in_maintained_space(src, self._customer_id, self._client.user):
                return ToolResult(
                    success=False,
                    output=f"DENIED: source_uri outside your memory space: {src}",
                )

        content, content_changes = _sanitize_user_facing_text(
            content,
            self._team_name,
        )
        content = _strip_leading_front_matter(content)
        now = datetime.now(timezone.utc)
        # Preserve the target's category if present in its path segment.
        category = "pattern"
        seg = _user_relative_segments(target_uri, self._client.user) or []
        base = seg[1:] if seg[:1] == ["memories"] else (seg[3:] if len(seg) > 3 else [])
        if base and len(base) >= 2:
            category = base[0]
        archived_list = [s for s in sources if s != target_uri]
        full_content = (
            f"---\n"
            f"category: {category}\n"
            f"created_by: memory-maintainer\n"
            f"created_at: {now.isoformat()}\n"
            f"replaces: {json.dumps(archived_list, ensure_ascii=False)}\n"
            f"---\n\n"
            f"{content}"
        )
        changed_paths = [target_uri]
        for source in archived_list:
            changed_paths.extend((source, _archived_uri(source)))
        change = _prepare_memory_change(
            self._change_ledger,
            action="merge",
            target_paths=changed_paths,
            source_refs=archived_list,
            reason=reason,
            before_path=target_uri,
            after_path=target_uri,
        )

        # Step 1: write the survivor. Abort the whole merge if this fails so no
        # source is archived against a stale/absent survivor.
        try:
            write = self._client.post(
                "/api/v1/content/write",
                json={"uri": target_uri, "content": full_content, "mode": "replace"},
            )
        except Exception as e:
            error = f"survivor write failed: {e}"
            metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="failed",
                error=error,
            )
            return ToolResult(
                success=False,
                output="",
                metadata=metadata,
                error=error,
            )
        if write.status_code != 200:
            error = (
                f"survivor write HTTP {write.status_code}: "
                f"{write.text[:200]}; no sources archived"
            )
            metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="failed",
                error=error,
                metadata={"write_status": write.status_code},
            )
            return ToolResult(
                success=False,
                output=f"FAILED: {error}",
                metadata=metadata,
            )

        # Step 2: archive each source (skip the survivor itself).
        archived: List[str] = []
        failed: List[str] = []
        for src in sources:
            if src == target_uri:
                continue
            try:
                mv = self._client.post(
                    "/api/v1/fs/mv",
                    json={"from_uri": src, "to_uri": _archived_uri(src)},
                )
                if mv.status_code in (200, 404):
                    archived.append(src)
                    if self._blackboard is not None:
                        self._blackboard.mark_processed(src, "merged", f"into {target_uri}")
                else:
                    failed.append(f"{src} (HTTP {mv.status_code})")
            except Exception as e:  # noqa: BLE001 - report per-source, keep going
                failed.append(f"{src} ({e})")

        note = f"; sanitized: {sorted(set(content_changes))}" if content_changes else ""
        summary = (
            f"OK: merged into {target_uri} (reason: {reason}); "
            f"archived {len(archived)}/{len(archived_list)} sources{note}"
        )
        if failed:
            # Survivor is safe; some sources remain. Surface them so the LLM can
            # retry archiving rather than assuming a clean merge.
            change_metadata = _finish_memory_change(
                self._change_ledger,
                change,
                result="partial",
                error="; ".join(failed),
                metadata={
                    "archived_count": len(archived),
                    "failed_count": len(failed),
                },
            )
            return ToolResult(
                success=True,
                output=summary + f"; NOT archived (retry viking_forget): {failed}",
                metadata={
                    "archived": archived,
                    "failed": failed,
                    **change_metadata,
                },
            )
        change_metadata = _finish_memory_change(
            self._change_ledger,
            change,
            result="applied",
            metadata={
                "archived_count": len(archived),
                "failed_count": 0,
            },
        )
        return ToolResult(
            success=True,
            output=summary,
            metadata={"archived": archived, **change_metadata},
        )
