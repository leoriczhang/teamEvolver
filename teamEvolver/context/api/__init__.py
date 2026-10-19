"""Agent Context gateway backed by incremental tenant/user PG records."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from ...openviking import OpenVikingClient, OpenVikingError, ensure_directories
from ...platform_docs.workspace_core import _normalize_entries, _scope_map, _validate_uri
from ...security.agent_principal import resolve_agent_principal
from ..repository import ContextRepository, stable_hash

SCOPES = ("personal_memory", "team_memory", "personal_skills", "team_skills", "personal_resources", "team_resources")


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 256 * 1024:
            raise HTTPException(413, "Context body exceeds 256 KiB")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "invalid Context JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "body must be an object")
    return value


def create_router(services=None, **kwargs) -> APIRouter:
    router = APIRouter(prefix="/internal/agents/context", tags=["agent-context"])
    if services is None or services.extras.get("config") is None or not services.extras.get("config_repo"):
        return router
    repo = ContextRepository(services.tx)
    config = services.extras["config"]

    def identity(request, value):
        principal = resolve_agent_principal(request, config, value.get("user_id"))
        from ...tenants.registry import effective_config, get_current_tenant
        cfg = effective_config(None, get_current_tenant(), config)
        scopes = _scope_map(cfg, principal.user_id, is_admin=False, personal_user=principal.user_id)
        return principal, cfg, scopes

    def ov_client(p, cfg, scope):
        factory = services.extras.get("context_client_factory")
        return factory(p, scope) if factory else OpenVikingClient(
            endpoint=cfg.sharing_viking_endpoint, api_key=cfg.sharing_viking_api_key,
            account_id=p.account_id, user=scope.openviking_user,
        )

    async def ov(p, cfg, scope, method, path, *, payload=None, params=None, key=""):
        client = ov_client(p, cfg, scope)
        try:
            result = await client.request(method, path, json_body=payload, params=params, idempotency_key=key)
            if not result.ok:
                raise HTTPException(result.status_code, f"OpenViking HTTP {result.status_code}")
            return result.data
        finally:
            await client.aclose()

    async def ensure(p, cfg, scope, *uris):
        client = ov_client(p, cfg, scope)
        try:
            try:
                await ensure_directories(client, uris)
            except OpenVikingError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
        finally:
            await client.aclose()

    def text(value):
        if isinstance(value, dict):
            value = value.get("content") or value.get("text") or ""
        result = str(value or "")
        if len(result.encode()) > 500_000:
            raise HTTPException(413, "Context read exceeds 500 KiB")
        return result

    async def skill_metadata(p, cfg, name, uri):
        reader = services.extras.get("skill_metadata_reader")
        raw = ""
        modified = ""
        try:
            raw = text(await ov(p, cfg, scopes_for(p, cfg)[name], "GET", "/api/v1/content/read",
                                params={"uri": uri + "/SKILL.md"}))
            stat = await ov(p, cfg, scopes_for(p, cfg)[name], "GET", "/api/v1/fs/stat", params={"uri": uri})
            if isinstance(stat, dict):
                modified = str(stat.get("modTime") or stat.get("modified_at") or "")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        meta = reader(raw) if reader else None
        return {**(meta or {"name": uri.rsplit("/", 1)[-1], "description": ""}), "modified_at": modified}

    def scopes_for(p, cfg):
        return _scope_map(cfg, p.user_id, is_admin=False, personal_user=p.user_id)

    @router.get("/describe")
    async def describe(request: Request, user_id: str = ""):
        p, cfg, scopes = identity(request, {"user_id": user_id})
        return {"protocol_version": "2.0", "subject": {"tenant_id": p.tenant_id, "user_id": p.user_id},
                "scopes": {name: {"kind": scopes[name].kind, "space": scopes[name].space,
                                  "operations": ["resolve", "read"] + (
                                      ["remember", "forget"] if name == "personal_memory" else [])}
                           for name in SCOPES},
                "budgets": {"max_items": 50, "max_chars": 100_000, "max_skill_bytes": 500_000}}

    @router.post("/sessions/start")
    async def start(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        external = str(value.get("external_session_id") or "").strip()
        if not external or len(external) > 1000:
            raise HTTPException(400, "external_session_id required (max 1000 characters)")
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            row, created = await repo.start_session(conn, p, external)
            if not row["ov_created"]:
                await ov(p, cfg, scopes["personal_memory"], "POST", "/api/v1/sessions",
                         payload={"session_id": row["ov_session_id"]}, key=row["session_id"])
                await repo.mark_created(conn, p, row["session_id"])
        return {"context_session_id": row["session_id"], "created": created}

    @router.post("/sessions/append")
    async def append(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        sid, eid = str(value.get("context_session_id") or ""), str(value.get("event_id") or "")
        role, content = value.get("role"), str(value.get("content") or "")
        if not eid or len(eid) > 200 or role not in {"user", "assistant", "system", "tool"}:
            raise HTTPException(400, "invalid context event")
        if len(content.encode()) > 128 * 1024:
            raise HTTPException(413, "context event too large")
        try:
            sequence = int(value.get("sequence") or 0)
            payload = {"role": role, "content": content}
            if value.get("created_at"):
                payload["created_at"] = value["created_at"]
            async with services.tx.tenant_tx(p.tenant_id) as conn:
                row, duplicate = await repo.event_status(conn, p, sid, eid, sequence, payload)
                if not duplicate:
                    await ov(p, cfg, scopes["personal_memory"], "POST",
                             f"/api/v1/sessions/{row['ov_session_id']}/messages",
                             payload=payload, key=stable_hash([sid, eid]))
                    await repo.record_event(conn, p, sid, eid, sequence, payload)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"appended": True, "duplicate": duplicate, "sequence": sequence}

    @router.post("/sessions/commit")
    async def commit(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        sid = str(value.get("context_session_id") or "")
        refs = value.get("used_context_refs") or []
        if not isinstance(refs, list):
            raise HTTPException(400, "used_context_refs must be a list")
        try:
            async with services.tx.tenant_tx(p.tenant_id) as conn:
                row = await repo.session(conn, p, sid, lock=True)
                if row["committed"]:
                    return {"committed": True, "duplicate": True}
                records = await repo.usage_refs(conn, p, sid, refs)
                used = f"/api/v1/sessions/{row['ov_session_id']}/messages"
                parts = [{"type": "context", "context_type": kind, "uri": uri}
                         for kind, uri in sorted({(r["kind"], r["uri"]) for r in records})]
                payloads = [{"role": "assistant", "parts": parts}] if parts else []
                for payload in payloads:
                    key = stable_hash([sid, payload])
                    if not await repo.usage_submitted(conn, p, sid, key):
                        await ov(p, cfg, scopes["personal_memory"], "POST", used, payload=payload, key=key)
                        await repo.usage_submitted(conn, p, sid, key, mark=True)
                result = await ov(p, cfg, scopes["personal_memory"], "POST",
                                  f"/api/v1/sessions/{row['ov_session_id']}/commit", payload={}, key=sid + ":commit")
                await repo.commit(conn, p, sid, result)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"committed": True, "duplicate": False, "result_hash": stable_hash(result)}

    @router.post("/resolve")
    async def resolve(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        query = str(value.get("query") or "").strip()
        names = value.get("scopes", list(SCOPES))
        if not query or len(query) > 8000:
            raise HTTPException(400, "invalid context query")
        if not isinstance(names, list) or not names or any(name not in SCOPES for name in names):
            raise HTTPException(403, "CONTEXT_SCOPE_FORBIDDEN")
        try:
            limit = max(1, min(50, int(value.get("max_items", 12))))
            budget = max(500, min(100_000, int(value.get("max_chars", 16_000))))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "invalid context budget") from exc
        async def search(name):
            try:
                data = await ov(p, cfg, scopes[name], "POST", "/api/v1/search/search",
                                payload={"query": query, "target_uri": scopes[name].root_uri, "limit": limit})
            except HTTPException as exc:
                if exc.status_code == 404:
                    return []
                raise
            def entries(data):
                if isinstance(data, list):
                    return [x for child in data for x in entries(child)]
                if isinstance(data, dict):
                    found = [{**data, "uri": data.get("uri") or data["path"]}] if (
                        data.get("uri") or data.get("path")) else []
                    return found + [x for k, child in data.items() if k != "uri" for x in entries(child)]
                return []
            return entries(data)
        results = await asyncio.gather(*(search(name) for name in names))
        effective, other = {}, []
        for name, rows in zip(names, results):
            for entry in rows:
                try:
                    uri = _validate_uri(scopes[name], entry["uri"], allow_root=False)
                except HTTPException:
                    continue
                if scopes[name].kind.rstrip("s") != "skill":
                    other.append((name, {**entry, "uri": uri}))
                    continue
                prefix = scopes[name].root_uri.rstrip("/") + "/"
                root = prefix + uri[len(prefix):].split("/")[0]
                meta = await skill_metadata(p, cfg, name, root)
                key = (meta["name"], meta["description"])
                prior = effective.get(key)
                priority = (meta["modified_at"], name == "personal_skills")
                if prior is None or priority > prior[0]:
                    effective[key] = (priority, name, {**entry, "uri": root, "skill_metadata": meta})
        results = [[e for n, e in other if n == name] + [e for _, n, e in effective.values() if n == name]
                   for name in names]
        sid = str(value.get("context_session_id") or "")
        items, used = [], 0
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            try:
                if sid:
                    await repo.session(conn, p, sid)
                seen = set()
                for index in range(max((len(r) for r in results), default=0)):
                    for name, rows in zip(names, results):
                        if index >= len(rows) or len(items) >= limit or used >= budget:
                            continue
                        entry = rows[index]
                        try:
                            uri = _validate_uri(scopes[name], entry["uri"], allow_root=False)
                        except HTTPException:
                            continue
                        kind = scopes[name].kind.rstrip("s")
                        if kind == "skill":
                            prefix = scopes[name].root_uri.rstrip("/") + "/"
                            slug = uri[len(prefix):].split("/")[0]
                            if slug.startswith("."):
                                continue
                            uri = prefix + slug
                        if (name, uri) in seen:
                            continue
                        seen.add((name, uri))
                        l0 = str(entry.get("abstract") or entry.get("summary") or entry.get("content") or "")[
                            :min(1000, budget - used)]
                        l1 = str(entry.get("overview") or "")[:min(4000, budget - used - len(l0))]
                        used += len(l0) + len(l1)
                        rid, receipt = await repo.issue_ref(conn, p, session_id=sid, scope=name, uri=uri, kind=kind,
                                                           version=str(entry.get("version") or ""))
                        items.append({"context_ref": rid, "receipt": receipt, "scope": name, "kind": kind,
                                      "title": uri.rsplit("/", 1)[-1], "l0": l0,
                                      "l1": l1, "skill_metadata": entry.get("skill_metadata", {}),
                                      "content_hash": stable_hash(l0), "selected": True,
                                      "path_alias": name + ":" + stable_hash(uri)[:12]})
                snap = "ctxsnap_" + stable_hash([p.tenant_id, p.user_id, sid, items])[:32]
                await repo.save_snapshot(conn, p, sid, snap, items)
            except (KeyError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"schema_version": "teamevolver.context-result", "snapshot_id": snap, "items": items,
                "subject": {"tenant_id": p.tenant_id, "user_id": p.user_id},
                "receipts": [{"context_ref": i["context_ref"], **i["receipt"]} for i in items],
                "warnings": [], "skills_etag": stable_hash(items),
                "budget": {"max_items": limit, "max_chars": budget, "used_items": len(items), "used_chars": used,
                           "truncated": len(items) >= limit or used >= budget}}

    @router.post("/read")
    async def read(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        rid, level = str(value.get("context_ref") or ""), str(value.get("level") or "l1")
        if level not in {"l0", "l1", "l2", "full"}:
            raise HTTPException(400, "unsupported content level")
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            ref = await repo.ref(conn, p, rid)
        if ref is None:
            raise HTTPException(404, "CONTEXT_REF_INVALID")
        scope = scopes[ref["scope"]]
        uri = _validate_uri(scope, ref["uri"], allow_root=False)
        if ref["kind"] == "skill" and level == "full":
            tree = await ov(p, cfg, scope, "GET", "/api/v1/fs/tree",
                            params={"uri": uri, "node_limit": 200, "level_limit": 8, "output": "original"})
            content = {}
            size = 0
            for item in _normalize_entries(tree)[:100]:
                path = item["uri"]
                if item.get("is_dir") or not path.startswith(uri + "/"):
                    continue
                _validate_uri(scope, path, allow_root=False)
                result = text(await ov(p, cfg, scope, "GET", "/api/v1/content/read", params={"uri": path}))
                size += len(result.encode())
                if size > 500_000:
                    raise HTTPException(413, "Skill bundle exceeds read budget")
                content[path[len(uri)+1:]] = result
            payload = {"bundle": content}
        else:
            path = {"l0": "abstract", "l1": "overview", "l2": "read", "full": "read"}[level]
            content = text(await ov(p, cfg, scope, "GET", "/api/v1/content/" + path, params={"uri": uri}))
            payload = {"content": content}
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            await repo.record_read(conn, p, rid, level, content)
        return {"context_ref": rid, "scope": ref["scope"], "kind": ref["kind"], "level": level, **payload}

    @router.get("/skills")
    async def skills(request: Request, user_id: str = "", scope: str = "all", context_session_id: str = ""):
        p, cfg, scopes = identity(request, {"user_id": user_id})
        names = {"personal": ["personal_skills"], "team": ["team_skills"],
                 "all": ["personal_skills", "team_skills"]}.get(scope)
        if names is None:
            raise HTTPException(400, "unsupported skill scope")
        items = []
        effective = {}
        for name in names:
            await ensure(p, cfg, scopes[name], scopes[name].root_uri)
            tree = await ov(p, cfg, scopes[name], "GET", "/api/v1/fs/tree",
                            params={"uri": scopes[name].root_uri, "node_limit": 2000, "level_limit": 2,
                                    "output": "original"})
            prefix = scopes[name].root_uri.rstrip("/") + "/"
            roots = {entry["uri"][len(prefix):].split("/")[0] for entry in _normalize_entries(tree)
                     if entry["uri"].startswith(prefix)}
            for slug in sorted(roots):
                if not slug or slug.startswith("."):
                    continue
                meta = await skill_metadata(p, cfg, name, prefix + slug)
                key = (meta["name"], meta["description"])
                priority = (meta["modified_at"], name == "personal_skills")
                if key not in effective or priority > effective[key][0]:
                    effective[key] = (priority, name, prefix + slug, meta)
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            for _, name, root, meta in effective.values():
                slug = root.rsplit("/", 1)[-1]
                rid, receipt = await repo.issue_ref(conn, p, session_id=context_session_id, scope=name,
                                                   uri=root, kind="skill")
                items.append({**meta, "scope": name, "context_ref": rid, "receipt": receipt,
                              "qualified_skill_id": ("personal" if name.startswith("personal") else "team")
                                                    + ":" + slug})
        snap = "ctxsnap_" + stable_hash(items)[:32]
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            await repo.save_snapshot(conn, p, context_session_id, snap, items)
        return {"skills": items, "snapshot_id": snap, "etag": stable_hash(items)}

    @router.post("/remember")
    async def remember(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        content = str(value.get("content") or "").strip()
        if not content or len(content.encode()) > 128 * 1024:
            raise HTTPException(400, "invalid memory content")
        scope = scopes["personal_memory"]
        key = stable_hash([p.tenant_id, p.user_id, value.get("idempotency_key") or content])
        uri = scope.root_uri.rstrip("/") + "/agent/" + key[:32] + ".md"
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            if value.get("context_session_id"):
                try:
                    await repo.session(conn, p, value["context_session_id"])
                except KeyError as exc:
                    raise HTTPException(409, str(exc)) from exc
            await ensure(p, cfg, scope, uri.rsplit("/", 1)[0])
            await ov(p, cfg, scope, "POST", "/api/v1/content/write",
                     payload={"uri": uri, "content": content, "mode": "replace", "wait": False}, key=key)
            rid, receipt = await repo.issue_ref(conn, p, session_id=value.get("context_session_id", ""),
                                               scope="personal_memory", uri=uri, kind="memory")
        return {"remembered": True, "context_ref": rid, "receipt": receipt}

    @router.post("/forget")
    async def forget(request: Request):
        value = await body(request)
        p, cfg, scopes = identity(request, value)
        rid = str(value.get("context_ref") or "")
        async with services.tx.tenant_tx(p.tenant_id) as conn:
            ref = await repo.ref(conn, p, rid)
            if ref is None or ref["scope"] != "personal_memory":
                raise HTTPException(403, "CONTEXT_SCOPE_FORBIDDEN")
            try:
                await ov(p, cfg, scopes["personal_memory"], "DELETE", "/api/v1/fs",
                         params={"uri": ref["uri"], "recursive": "false"}, key=rid)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            await repo.revoke(conn, p, rid)
        return {"forgotten": True}

    return router
