"""Console memory retrieval debugger backed by the Agent Context contract."""
from __future__ import annotations
import asyncio
import hashlib
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .openviking_workspace import _OpenVikingRequestError, _validate_uri


def _entries(result: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, dict):
            if value.get("uri") or value.get("path"): found.append(value)
            for key, nested in value.items():
                if key not in {"uri", "path"} and isinstance(nested, (dict, list)): visit(nested)
    visit(result)
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in found:
        uri = str(item.get("uri") or item.get("path") or "").rstrip("/")
        if uri and uri not in seen:
            seen.add(uri)
            output.append(item)
    return output


def _text(value: Any, limit: int) -> str:
    if isinstance(value, dict):
        value = value.get("abstract") or value.get("summary") or value.get("content") or value.get("text") or ""
    return str(value or "")[:max(0, limit)]


class MemoryDebugMixin:
    """Preview personal and team memories exactly as Agent context."""
    def _register_memory_debug_routes(self, app: FastAPI) -> None:
        owner = self

        @app.post("/api/openviking/memory/debug")
        async def debug_memory_context(request: Request):
            body = await request.json()
            if not isinstance(body, dict): raise HTTPException(400, "body must be an object")
            user_id = str(body.get("user_id") or "")
            query = str(body.get("query") or "").strip()
            if not query or len(query) > 8000: raise HTTPException(400, "invalid memory query")
            try:
                max_items = max(1, min(50, int(body.get("max_items") or 12)))
                max_chars = max(500, min(100000, int(body.get("max_chars") or 16000)))
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "invalid memory budget") from exc
            resolved = [owner._workspace_scope(request, user_id, name) for name in ("personal_memory", "team_memory")]

            async def search(user, scope):
                try:
                    result = await owner._workspace_request(user, scope, "POST", "/api/v1/search/search", json={"query": query, "target_uri": scope.root_uri, "limit": max_items})
                except _OpenVikingRequestError as exc:
                    if exc.status_code == 404 or "NOT_FOUND" in str(exc): return []
                    raise HTTPException(exc.status_code, str(exc)) from exc
                return _entries(result)

            results = await asyncio.gather(*(search(user, scope) for user, scope in resolved))
            items: list[dict[str, Any]] = []
            used_chars = 0
            for (_user, scope), matches in zip(resolved, results):
                for match in matches:
                    if len(items) >= max_items or used_chars >= max_chars: break
                    try: uri = _validate_uri(scope, match.get("uri") or match.get("path"), allow_root=False)
                    except HTTPException: continue
                    l0 = _text(match, min(1000, max_chars - used_chars))
                    used_chars += len(l0)
                    alias = f"{scope.name}:{hashlib.sha256(uri.encode()).hexdigest()[:12]}"
                    items.append({"scope": scope.name, "space": scope.space, "kind": "memory", "title": str(match.get("name") or match.get("title") or uri.rsplit("/", 1)[-1]), "path_alias": alias, "l0": l0, "l1": _text(match.get("overview"), 4000), "score": match.get("score"), "selected": True, "provenance": {"space": scope.space}})
            rendered = "\n\n".join(f"## {item['title']}\nscope: {item['scope']}\npath: {item['path_alias']}\n\n{item['l0'] or item['l1'] or '（无可注入摘要）'}" for item in items)
            return JSONResponse(content={"schema_version": "teamevolver.memory-debug.v1", "user_id": user_id, "query": query, "scopes": ["personal_memory", "team_memory"], "items": items, "agent_context": rendered, "budget": {"max_items": max_items, "max_chars": max_chars, "used_items": len(items), "used_chars": used_chars, "truncated": len(items) >= max_items or used_chars >= max_chars}})
