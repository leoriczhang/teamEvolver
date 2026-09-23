"""Agent-facing Context Workspace API."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..integrations.agent_protocol import CONTEXT_RESULT_SCHEMA_V1
from ..integrations.agent_principal import AgentPrincipal
from ..integrations.legacy_agent_identity import resolve_request_principal
from ..integrations.context_workspace import ContextStateStore, stable_hash
from ..tenants.registry import AGENT_TOKEN_PREFIX, get_current_tenant
from team_skills.library.frontmatter import parse_skill_md_text
from .openviking_workspace import (
    _normalize_entries,
    _OpenVikingRequestError,
    _scope_map,
    _validate_uri,
)

_AGENT_SCOPES = (
    "personal_memory",
    "team_memory",
    "personal_skills",
    "team_skills",
    "personal_resources",
    "team_resources",
)
_MAX_QUERY_CHARS = 8_000
_MAX_REMEMBER_BYTES = 128 * 1024
_MAX_READ_CHARS = 500_000


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    return header[7:].strip() if header.lower().startswith("bearer ") else header


def _search_entries(result: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        uri = str(value.get("uri") or value.get("path") or "").strip()
        if uri:
            entries.append(value)
        for key, nested in value.items():
            if key not in {"uri", "path"} and isinstance(nested, (dict, list)):
                visit(nested)

    visit(result)
    seen: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for entry in entries:
        uri = str(entry.get("uri") or entry.get("path") or "").rstrip("/")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        deduplicated.append(entry)
    return deduplicated


def _text(value: Any, limit: int) -> str:
    if isinstance(value, dict):
        value = value.get("content") or value.get("text") or ""
    return str(value or "")[: max(0, limit)]


def _interleave_scope_entries(
    scope_names: list[str],
    results: list[list[dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Preserve per-scope ranking without letting one scope consume the budget."""
    scoped = list(zip(scope_names, results))
    depth = max((len(entries) for _name, entries in scoped), default=0)
    return [
        (scope_name, entries[index])
        for index in range(depth)
        for scope_name, entries in scoped
        if index < len(entries)
    ]


def _skill_root_uri(scope_root: str, uri: str) -> str:
    """Return the ``.../skills/<slug>`` root for a skill file hit, or ``""``.

    OpenViking semantic search returns individual files inside a skill
    (``SKILL.md``, ``versions/vN/SKILL.md``, ``.abstract.md`` …). A skill's
    identity is its first path segment under the skills root, never the matched
    file name — otherwise every skill collides on ``SKILL.md``.
    """
    prefix = scope_root.rstrip("/") + "/"
    cleaned = str(uri or "").rstrip("/")
    if not cleaned.startswith(prefix):
        return ""
    slug = cleaned[len(prefix):].split("/", 1)[0]
    # Hidden metadata files living directly under the skills root
    # (".overview.md", ".abstract.md" ...) are not skills.
    if not slug or slug.startswith("."):
        return ""
    return f"{prefix}{slug}"



class AgentContextMixin:
    """Expose scoped OpenViking context without sharing storage credentials."""

    def _agent_context_auth(self, request: Request) -> None:
        """Authenticate a Context Workspace caller.

        The only machine credential is the tenant credential (``tevt_``): the
        tenant is resolved by middleware; the request declares its user_id.
        """
        token = _bearer_token(request)
        if (
            token.startswith(AGENT_TOKEN_PREFIX)
            and getattr(request.state, "tenant_source", "") == "token"
            and get_current_tenant() is not None
        ):
            return
        raise HTTPException(
            status_code=401,
            detail="WORKSPACE_TOKEN_INVALID",
        )

    def _agent_context_identity(self, request: Request, body: dict) -> tuple[AgentPrincipal, dict]:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        principal = resolve_request_principal(request, self._workspace_config(), body)
        user = {
            "id": principal.user_id,
            "personal_space": {"viking_user": principal.user_id},
            "_agent_principal": principal,
        }
        legacy_user = getattr(request.state, "agent_legacy_user", None)
        if legacy_user is not None:
            user["personal_space"] = legacy_user.get("personal_space") or {"viking_user": principal.user_id}
        return principal, user

    def _agent_context_scopes(
        self,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        personal_space = (
            user.get("personal_space")
            if isinstance(user.get("personal_space"), dict)
            else {}
        )
        return _scope_map(
            self._workspace_config(),
            str(user.get("id") or ""),
            is_admin=False,
            personal_user=str(personal_space.get("viking_user") or ""),
        )

    async def _agent_context_search(
        self,
        user: dict[str, Any],
        scope: Any,
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            result = await self._workspace_request(
                user,
                scope,
                "POST",
                "/api/v1/search/search",
                json={
                    "query": query,
                    "target_uri": scope.root_uri,
                    "limit": limit,
                },
            )
        except _OpenVikingRequestError as exc:
            if exc.status_code == 404 or "NOT_FOUND" in str(exc):
                return []
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return _search_entries(result)

    async def _agent_skill_meta(
        self,
        user: dict[str, Any],
        scope: Any,
        *,
        skill_root_uri: str,
    ) -> dict[str, str]:
        """Resolve a skill root's identity + recency for cross-scope dedup.

        Returns ``{name, description, modified_at}``. ``name``/``description``
        come from the root ``SKILL.md`` frontmatter (the skill's true identity,
        not the matched file name); ``modified_at`` is the root directory's
        ``modTime`` so duplicates can be resolved latest-wins. Falls back to the
        root slug with an empty description/mtime when metadata is unavailable,
        so a skill still resolves.
        """
        slug = skill_root_uri.rstrip("/").rsplit("/", 1)[-1]
        name, description = slug, ""
        try:
            value = await self._agent_context_read_value(
                user,
                scope,
                uri=f"{skill_root_uri.rstrip('/')}/SKILL.md",
                level="full",
            )
        except HTTPException:
            value = None
        parsed = parse_skill_md_text(_text(value, _MAX_READ_CHARS)) if value else None
        if parsed:
            name, description = parsed["name"], parsed["description"]
        modified_at = ""
        try:
            stat = await self._workspace_request(
                user,
                scope,
                "GET",
                "/api/v1/fs/stat",
                params={"uri": skill_root_uri},
            )
        except _OpenVikingRequestError:
            stat = None
        if isinstance(stat, dict):
            modified_at = str(stat.get("modTime") or stat.get("modified_at") or "")
        return {"name": name, "description": description, "modified_at": modified_at}

    async def _agent_context_read_value(
        self,
        user: dict[str, Any],
        scope: Any,
        *,
        uri: str,
        level: str,
    ) -> Any:
        path = {
            "l0": "/api/v1/content/abstract",
            "l1": "/api/v1/content/overview",
            "l2": "/api/v1/content/read",
            "full": "/api/v1/content/read",
        }[level]
        params: dict[str, Any] = {"uri": uri}
        if level in {"l2", "full"}:
            params.update({"offset": 0, "limit": -1, "raw": "true"})
        try:
            return await self._workspace_request(
                user,
                scope,
                "GET",
                path,
                params=params,
            )
        except _OpenVikingRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    async def _agent_context_skill_bundle(
        self,
        user: dict[str, Any],
        scope: Any,
        *,
        uri: str,
    ) -> dict[str, str]:
        try:
            tree = await self._workspace_request(
                user,
                scope,
                "GET",
                "/api/v1/fs/tree",
                params={
                    "uri": uri,
                    "output": "original",
                    "show_all_hidden": "false",
                    "level_limit": 8,
                    "node_limit": 200,
                },
            )
        except _OpenVikingRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        files = [
            item
            for item in _normalize_entries(tree)
            if not item.get("is_dir")
            and str(item.get("uri") or "").startswith(f"{uri.rstrip('/')}/")
        ]
        bundle: dict[str, str] = {}
        total = 0
        for item in files[:100]:
            file_uri = _validate_uri(scope, item["uri"], allow_root=False)
            value = await self._agent_context_read_value(
                user,
                scope,
                uri=file_uri,
                level="full",
            )
            content = _text(value, _MAX_READ_CHARS - total)
            total += len(content)
            relative = file_uri[len(uri.rstrip("/")) :].lstrip("/")
            bundle[relative] = content
            if total >= _MAX_READ_CHARS:
                break
        return bundle

    async def _agent_context_submit_usage(
        self,
        *,
        state: ContextStateStore,
        user: dict[str, Any],
        personal_scope: Any,
        session: dict[str, Any],
        principal: AgentPrincipal,
        ref_ids: list[str],
    ) -> dict[str, int]:
        try:
            records = await asyncio.to_thread(
                state.resolve_session_usage_refs,
                str(session["context_session_id"]),
                principal=principal,
                ref_ids=ref_ids,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        submitted = {
            str(item)
            for item in session.get("submitted_usage_keys") or []
            if str(item)
        }
        used_path = f"/api/v1/sessions/{session['openviking_session_id']}/used"
        contexts = sorted(
            {
                str(record.get("uri") or "")
                for record in records
                if record.get("kind") == "memory" and record.get("uri")
            }
        )
        submitted_count = 0
        skipped_count = 0
        if contexts:
            payload = {"contexts": contexts}
            usage_key = "contexts:" + stable_hash(payload)
            if usage_key in submitted:
                skipped_count += len(contexts)
            else:
                await self._workspace_request(
                    user,
                    personal_scope,
                    "POST",
                    used_path,
                    json=payload,
                )
                await asyncio.to_thread(
                    state.mark_usage_submitted,
                    str(session["context_session_id"]),
                    principal=principal,
                    usage_key=usage_key,
                )
                submitted.add(usage_key)
                submitted_count += len(contexts)

        skills = sorted(
            {
                str(record.get("uri") or "")
                for record in records
                if record.get("kind") == "skill" and record.get("uri")
            }
        )
        for skill_uri in skills:
            payload = {"skill": {"uri": skill_uri}}
            usage_key = "skill:" + stable_hash(payload)
            if usage_key in submitted:
                skipped_count += 1
                continue
            await self._workspace_request(
                user,
                personal_scope,
                "POST",
                used_path,
                json=payload,
            )
            await asyncio.to_thread(
                state.mark_usage_submitted,
                str(session["context_session_id"]),
                principal=principal,
                usage_key=usage_key,
            )
            submitted.add(usage_key)
            submitted_count += 1
        return {
            "contexts": len(contexts),
            "skills": len(skills),
            "submitted": submitted_count,
            "skipped": skipped_count,
        }

    def _register_agent_context_routes(self, app: FastAPI) -> None:
        owner = self

        def store(request: Request) -> ContextStateStore:
            legacy = getattr(request.state, "agent_legacy_record", {})
            return ContextStateStore(owner._workspace_config(), legacy_agent_id=legacy.get("agent_id", ""))

        @app.get("/internal/agents/context/describe")
        async def agent_context_describe(
            request: Request,
            external_subject: str = Query(default=""),
            integration_id: str = Query(default=""),
            user_id: str | None = Query(default=None),
        ):
            owner._agent_context_auth(request)
            identity = {"external_subject": external_subject, "integration_id": integration_id}
            if user_id is not None:
                identity["user_id"] = user_id
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, identity)
            scopes = owner._agent_context_scopes(user)
            payload = {
                "protocol_version": "2.0",
                "subject": {"tenant_id": principal.tenant_id, "user_id": principal.user_id},
                "scopes": {
                    name: {
                        "kind": scopes[name].kind,
                        "space": scopes[name].space,
                        "operations": (
                            ["resolve", "read", "remember", "forget"]
                            if name == "personal_memory"
                            else ["resolve", "read"]
                        ),
                    }
                    for name in _AGENT_SCOPES
                },
                "budgets": {
                    "max_items": 50,
                    "max_chars": 100_000,
                    "max_skill_bytes": _MAX_READ_CHARS,
                },
            }
            if getattr(request.state, "agent_legacy_identity", False):
                payload.update(protocol_version="1.0", integration_id=integration_id)
            await asyncio.to_thread(
                store(request).audit,
                action="describe",
                principal=principal,
            )
            return JSONResponse(content=payload)

        @app.post("/internal/agents/context/resolve")
        async def agent_context_resolve(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be an object")
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            query = str(body.get("query") or "").strip()
            if not query or len(query) > _MAX_QUERY_CHARS:
                raise HTTPException(status_code=400, detail="invalid context query")
            raw_scopes = body.get("scopes")
            selected_names = (
                [str(item) for item in raw_scopes]
                if isinstance(raw_scopes, list)
                else list(_AGENT_SCOPES)
            )
            if not selected_names or any(
                name not in _AGENT_SCOPES for name in selected_names
            ):
                raise HTTPException(
                    status_code=403,
                    detail="CONTEXT_SCOPE_FORBIDDEN",
                )
            max_items = max(1, min(50, int(body.get("max_items") or 12)))
            max_chars = max(500, min(100_000, int(body.get("max_chars") or 16_000)))
            scopes = owner._agent_context_scopes(user)
            results = await asyncio.gather(
                *[
                    owner._agent_context_search(
                        user,
                        scopes[name],
                        query=query,
                        limit=max_items,
                    )
                    for name in selected_names
                ]
            )
            state = store(request)
            session_id = str(body.get("context_session_id") or "")
            if session_id and await asyncio.to_thread(state.get_session, session_id, principal=principal) is None:
                raise HTTPException(status_code=409, detail="context session subject binding conflict")
            items: list[dict[str, Any]] = []
            used_chars = 0
            # Cross-scope skill dedup keyed on identity (name + description),
            # not folder slug: personal and team copies of the same evolved
            # skill must collapse to one selected entry, latest wins.
            effective_skills: dict[tuple[str, str], dict[str, Any]] = {}
            seen_scope_roots: set[tuple[str, str]] = set()
            warnings: list[dict[str, str]] = []
            for scope_name, entry in _interleave_scope_entries(
                selected_names,
                results,
            ):
                if len(items) >= max_items or used_chars >= max_chars:
                    break
                selected_scope = scopes[scope_name]
                try:
                    uri = _validate_uri(
                        selected_scope,
                        entry.get("uri") or entry.get("path"),
                        allow_root=False,
                    )
                except HTTPException:
                    continue
                kind = selected_scope.kind.rstrip("s")
                skill_meta: dict[str, str] | None = None
                if kind == "skill":
                    root_uri = _skill_root_uri(selected_scope.root_uri, uri)
                    if not root_uri:
                        continue
                    uri = root_uri
                    # Collapse the several file hits (SKILL.md, versions/…) that
                    # a single skill emits into one entry per scope root.
                    scope_key = (scope_name, uri)
                    if scope_key in seen_scope_roots:
                        continue
                    seen_scope_roots.add(scope_key)
                    skill_meta = await owner._agent_skill_meta(
                        user,
                        selected_scope,
                        skill_root_uri=uri,
                    )
                    name = skill_meta["name"]
                else:
                    name = str(
                        entry.get("name")
                        or entry.get("title")
                        or uri.rsplit("/", 1)[-1]
                    )
                l0 = _text(
                    entry.get("abstract")
                    or entry.get("summary")
                    or entry.get("content"),
                    min(1_000, max_chars - used_chars),
                )
                used_chars += len(l0)
                context_ref, receipt = await asyncio.to_thread(
                    state.issue_ref,
                    principal=principal,
                    session_id=session_id,
                    scope=scope_name,
                    uri=uri,
                    kind=kind,
                    version=str(entry.get("version") or entry.get("sha256") or ""),
                )
                item = {
                    "scope": scope_name,
                    "kind": kind,
                    "context_ref": context_ref,
                    "receipt": receipt,
                    "title": name,
                    "path_alias": f"{scope_name}:{stable_hash(uri)[:12]}",
                    "l0": l0,
                    "l1": _text(entry.get("overview"), 4_000),
                    "version": str(entry.get("version") or ""),
                    "content_hash": str(
                        entry.get("sha256")
                        or entry.get("hash")
                        or stable_hash(l0)
                    ),
                    "provenance": {
                        "tenant_id": principal.tenant_id,
                        "space": selected_scope.space,
                    },
                    "selected": True,
                }
                if kind == "skill" and skill_meta is not None:
                    qualified = (
                        "personal" if scope_name == "personal_skills" else "team"
                    ) + f":{name}"
                    item["qualified_skill_id"] = qualified
                    item["modified_at"] = skill_meta["modified_at"]
                    # Identity is name + description. A team copy that only
                    # differs by folder slug still collides here.
                    identity = (name, skill_meta["description"])
                    previous = effective_skills.get(identity)
                    if previous is None:
                        effective_skills[identity] = item
                    else:
                        # Latest wins; a modTime tie favours the caller's own
                        # personal copy so behaviour is deterministic.
                        item_mtime = str(item["modified_at"])
                        prev_mtime = str(previous.get("modified_at") or "")
                        if item_mtime == prev_mtime:
                            newer = scope_name == "personal_skills"
                        else:
                            newer = item_mtime > prev_mtime
                        loser, winner = (
                            (previous, item) if newer else (item, previous)
                        )
                        loser["selected"] = False
                        loser["shadowed_by"] = winner["qualified_skill_id"]
                        winner["selected"] = True
                        winner.pop("shadowed_by", None)
                        if newer:
                            effective_skills[identity] = item
                        warnings.append(
                            {
                                "code": "DUPLICATE_SKILL",
                                "skill_name": name,
                                "selected": winner["qualified_skill_id"],
                                "shadowed": loser["qualified_skill_id"],
                            }
                        )
                items.append(item)
            snapshot_id = "ctxsnap_" + stable_hash(
                {
                    "tenant_id": principal.tenant_id,
                    "user": user.get("id"),
                    "session": session_id,
                    "query": query,
                    "refs": [item["context_ref"] for item in items],
                }
            )[:32]
            await asyncio.to_thread(
                state.save_snapshot,
                snapshot_id=snapshot_id,
                principal=principal,
                session_id=session_id,
                items=items,
            )
            await asyncio.to_thread(
                state.audit,
                action="resolve",
                principal=principal,
                session_id=session_id,
                result=f"{len(items)} items",
            )
            return JSONResponse(
                content={
                    "schema_version": (
                        CONTEXT_RESULT_SCHEMA_V1 if getattr(request.state, "agent_legacy_identity", False)
                        else "teamevolver.context-result.v2"
                    ),
                    "subject": {
                        "tenant_id": principal.tenant_id,
                        "user_id": principal.user_id,
                        **(
                            {
                                "integration_id": request.state.agent_legacy_record.get("agent_id"),
                                "runtime_type": request.state.agent_legacy_record.get("runtime_type"),
                                "session_id": session_id,
                            }
                            if getattr(request.state, "agent_legacy_identity", False) else {}
                        ),
                    },
                    "snapshot_id": snapshot_id,
                    "items": items,
                    "receipts": [
                        {"context_ref": item["context_ref"], **item["receipt"]}
                        for item in items
                    ],
                    "warnings": warnings,
                    "budget": {
                        "max_items": max_items,
                        "max_chars": max_chars,
                        "used_items": len(items),
                        "used_chars": used_chars,
                        "truncated": len(items) >= max_items or used_chars >= max_chars,
                    },
                    "skills_etag": stable_hash(
                        [
                            item["content_hash"]
                            for item in items
                            if item["kind"] == "skill"
                        ]
                    ),
                }
            )

        @app.post("/internal/agents/context/read")
        async def agent_context_read(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            context_ref = str(body.get("context_ref") or "")
            level = str(body.get("level") or "l1").lower()
            if level not in {"l0", "l1", "l2", "full"}:
                raise HTTPException(status_code=400, detail="unsupported content level")
            state = store(request)
            ref = await asyncio.to_thread(
                state.resolve_ref,
                context_ref,
                principal=principal,
            )
            if ref is None:
                raise HTTPException(status_code=404, detail="CONTEXT_REF_INVALID")
            scope = owner._agent_context_scopes(user)[str(ref["scope"])]
            uri = _validate_uri(scope, ref["uri"], allow_root=False)
            if ref.get("kind") == "skill" and level == "full":
                bundle = await owner._agent_context_skill_bundle(
                    user,
                    scope,
                    uri=uri,
                )
                payload: dict[str, Any] = {"bundle": bundle}
                expanded_value: Any = bundle
            else:
                value = await owner._agent_context_read_value(
                    user,
                    scope,
                    uri=uri,
                    level=level,
                )
                expanded_value = _text(value, _MAX_READ_CHARS)
                payload = {"content": expanded_value}
            await asyncio.to_thread(
                state.record_snapshot_read,
                ref_id=context_ref,
                principal=principal,
                level=level,
                value=expanded_value,
            )
            await asyncio.to_thread(
                state.audit,
                action="read",
                principal=principal,
                session_id=str(ref.get("session_id") or ""),
                scope=str(ref.get("scope") or ""),
                uri_hash=str(ref.get("uri_hash") or ""),
            )
            return JSONResponse(
                content={
                    "context_ref": context_ref,
                    "scope": ref.get("scope"),
                    "kind": ref.get("kind"),
                    "level": level,
                    **payload,
                }
            )

        @app.get("/internal/agents/context/skills")
        async def agent_context_skills(
            request: Request,
            external_subject: str = Query(default=""),
            scope: str = Query(default="all"),
            context_session_id: str = Query(default=""),
            integration_id: str = Query(default=""),
            user_id: str | None = Query(default=None),
        ):
            owner._agent_context_auth(request)
            identity = {"external_subject": external_subject, "integration_id": integration_id}
            if user_id is not None:
                identity["user_id"] = user_id
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, identity)
            state = store(request)
            if context_session_id:
                session = await asyncio.to_thread(
                    state.get_session,
                    context_session_id,
                    principal=principal,
                )
                if (
                    session is None
                    or str(session.get("user_id") or "")
                    != str(user.get("id") or "")
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="context session subject binding conflict",
                    )
            names = {
                "personal": ["personal_skills"],
                "team": ["team_skills"],
                "all": ["personal_skills", "team_skills"],
            }.get(scope)
            if names is None:
                raise HTTPException(status_code=400, detail="unsupported skill scope")
            scopes = owner._agent_context_scopes(user)
            items: list[dict[str, Any]] = []
            for name in names:
                selected = scopes[name]
                try:
                    tree = await owner._workspace_request(
                        user,
                        selected,
                        "GET",
                        "/api/v1/fs/tree",
                        params={
                            "uri": selected.root_uri,
                            "output": "original",
                            "show_all_hidden": "false",
                            "level_limit": 2,
                            "node_limit": 2_000,
                        },
                    )
                except _OpenVikingRequestError as exc:
                    if exc.status_code == 404 or "NOT_FOUND" in str(exc):
                        continue
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail=str(exc),
                    ) from exc
                roots: dict[str, str] = {}
                for entry in _normalize_entries(tree):
                    uri = str(entry.get("uri") or "")
                    prefix = selected.root_uri.rstrip("/") + "/"
                    if not uri.startswith(prefix):
                        continue
                    skill_name = uri[len(prefix) :].split("/", 1)[0]
                    if skill_name:
                        roots.setdefault(skill_name, f"{prefix}{skill_name}")
                for skill_name, uri in sorted(roots.items()):
                    context_ref, receipt = await asyncio.to_thread(
                        state.issue_ref,
                        principal=principal,
                        session_id=context_session_id,
                        scope=name,
                        uri=uri,
                        kind="skill",
                    )
                    items.append(
                        {
                            "qualified_skill_id": (
                                "personal" if name == "personal_skills" else "team"
                            )
                            + f":{skill_name}",
                            "name": skill_name,
                            "scope": name,
                            "context_ref": context_ref,
                            "receipt": receipt,
                        }
                    )
            snapshot_id = ""
            if context_session_id and items:
                snapshot_id = "ctxsnap_" + stable_hash(
                    {
                        "tenant_id": principal.tenant_id,
                        "user": user.get("id"),
                        "session": context_session_id,
                        "inventory": [
                            item["context_ref"] for item in items
                        ],
                    }
                )[:32]
                await asyncio.to_thread(
                    state.save_snapshot,
                    snapshot_id=snapshot_id,
                    principal=principal,
                    session_id=context_session_id,
                    items=[
                        {
                            "context_ref": item["context_ref"],
                            "title": item["name"],
                        }
                        for item in items
                    ],
                )
            return JSONResponse(
                content={
                    "skills": items,
                    "snapshot_id": snapshot_id,
                    "etag": stable_hash(
                        [
                            (item["qualified_skill_id"], item["receipt"]["uri_hash"])
                            for item in items
                        ]
                    ),
                }
            )

        @app.post("/internal/agents/context/remember")
        async def agent_context_remember(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            context_session_id = str(body.get("context_session_id") or "")
            if context_session_id and await asyncio.to_thread(
                store(request).get_session, context_session_id, principal=principal,
            ) is None:
                raise HTTPException(status_code=409, detail="context session subject binding conflict")
            content = str(body.get("content") or "").strip()
            if not content or len(content.encode("utf-8")) > _MAX_REMEMBER_BYTES:
                raise HTTPException(status_code=400, detail="invalid memory content")
            category = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                str(body.get("category") or "agent").strip(),
            ).strip(".-") or "agent"
            scope = owner._agent_context_scopes(user)["personal_memory"]
            identity = str(
                body.get("idempotency_key")
                or stable_hash(
                    {
                        "tenant_id": principal.tenant_id,
                        "user": user.get("id"),
                        "content": content,
                    }
                )
            )
            uri = f"{scope.root_uri.rstrip('/')}/{category}/{stable_hash(identity)[:32]}.md"
            try:
                await owner._workspace_request(
                    user,
                    scope,
                    "POST",
                    "/api/v1/fs/mkdir",
                    json={
                        "uri": f"{scope.root_uri.rstrip('/')}/{category}",
                        "description": "Agent personal memories",
                    },
                )
            except _OpenVikingRequestError as exc:
                if exc.status_code not in {400, 409}:
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail=str(exc),
                    ) from exc
            await owner._workspace_request(
                user,
                scope,
                "POST",
                "/api/v1/content/write",
                json={
                    "uri": uri,
                    "content": content,
                    "mode": "replace",
                    "wait": False,
                },
            )
            state = store(request)
            context_ref, receipt = await asyncio.to_thread(
                state.issue_ref,
                principal=principal,
                session_id=str(body.get("context_session_id") or ""),
                scope="personal_memory",
                uri=uri,
                kind="memory",
            )
            await asyncio.to_thread(
                state.audit,
                action="remember",
                principal=principal,
                session_id=str(body.get("context_session_id") or ""),
                scope="personal_memory",
                uri_hash=receipt["uri_hash"],
            )
            return JSONResponse(
                content={
                    "remembered": True,
                    "context_ref": context_ref,
                    "receipt": receipt,
                }
            )

        @app.post("/internal/agents/context/forget")
        async def agent_context_forget(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            context_ref = str(body.get("context_ref") or "")
            state = store(request)
            ref = await asyncio.to_thread(
                state.resolve_ref,
                context_ref,
                principal=principal,
            )
            if ref is None or ref.get("scope") != "personal_memory":
                raise HTTPException(
                    status_code=403,
                    detail="CONTEXT_SCOPE_FORBIDDEN",
                )
            scope = owner._agent_context_scopes(user)["personal_memory"]
            uri = _validate_uri(scope, ref["uri"], allow_root=False)
            try:
                await owner._workspace_request(
                    user,
                    scope,
                    "DELETE",
                    "/api/v1/fs",
                    params={"uri": uri, "recursive": "false", "wait": "false"},
                )
            except _OpenVikingRequestError as exc:
                if exc.status_code != 404 and "NOT_FOUND" not in str(exc):
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail=str(exc),
                    ) from exc
            await asyncio.to_thread(state.revoke_ref, context_ref, principal=principal)
            await asyncio.to_thread(
                state.audit,
                action="forget",
                principal=principal,
                scope="personal_memory",
                uri_hash=str(ref.get("uri_hash") or ""),
            )
            return JSONResponse(content={"forgotten": True})

        @app.post("/internal/agents/context/sessions/start")
        async def agent_context_session_start(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            external_session_id = str(body.get("external_session_id") or "").strip()
            if not external_session_id:
                raise HTTPException(
                    status_code=400,
                    detail="external_session_id is required",
                )
            state = store(request)
            session, created = await asyncio.to_thread(
                state.start_session,
                principal=principal,
                external_session_id=external_session_id,
            )
            personal_scope = owner._agent_context_scopes(user)["personal_memory"]
            openviking_created = bool(session.get("openviking_created", False))
            if not openviking_created:
                should_create = created
                if not created:
                    try:
                        await owner._workspace_request(
                            user,
                            personal_scope,
                            "GET",
                            (
                                "/api/v1/sessions/"
                                f"{session['openviking_session_id']}"
                            ),
                        )
                    except _OpenVikingRequestError as exc:
                        if exc.status_code != 404 and "NOT_FOUND" not in str(exc):
                            raise
                        should_create = True
                if should_create:
                    await owner._workspace_request(
                        user,
                        personal_scope,
                        "POST",
                        "/api/v1/sessions",
                        json={"session_id": session["openviking_session_id"]},
                    )
                session = await asyncio.to_thread(
                    state.mark_openviking_created,
                    session["context_session_id"],
                    principal=principal,
                )
            await asyncio.to_thread(
                state.audit,
                action="session.start",
                principal=principal,
                session_id=session["context_session_id"],
                result="created" if created else "duplicate",
            )
            return JSONResponse(
                content={
                    "context_session_id": session["context_session_id"],
                    "created": created,
                }
            )

        @app.post("/internal/agents/context/sessions/append")
        async def agent_context_session_append(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            context_session_id = str(body.get("context_session_id") or "")
            event_id = str(body.get("event_id") or "").strip()
            sequence = int(body.get("sequence") or 0)
            role = str(body.get("role") or "").strip().lower()
            content = str(body.get("content") or "")
            if not event_id or role not in {"user", "assistant", "system", "tool"}:
                raise HTTPException(status_code=400, detail="invalid context event")
            if len(content.encode("utf-8")) > _MAX_REMEMBER_BYTES:
                raise HTTPException(status_code=413, detail="context event is too large")
            state = store(request)
            event_hash = stable_hash(
                {"sequence": sequence, "role": role, "content": content}
            )
            try:
                status = await asyncio.to_thread(
                    state.event_status,
                    context_session_id,
                    principal=principal,
                    event_id=event_id,
                    event_hash=event_hash,
                    sequence=sequence,
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if status == "duplicate":
                return JSONResponse(
                    content={"appended": True, "duplicate": True, "sequence": sequence}
                )
            session = await asyncio.to_thread(
                state.get_session,
                context_session_id,
                principal=principal,
            )
            if session is None:
                raise HTTPException(status_code=404, detail="context session not found")
            personal_scope = owner._agent_context_scopes(user)["personal_memory"]
            await owner._workspace_request(
                user,
                personal_scope,
                "POST",
                f"/api/v1/sessions/{session['openviking_session_id']}/messages",
                json={
                    "role": role,
                    "content": content,
                    **(
                        {"created_at": body["created_at"]}
                        if body.get("created_at")
                        else {}
                    ),
                },
            )
            await asyncio.to_thread(
                state.record_event,
                context_session_id,
                principal=principal,
                event_id=event_id,
                event_hash=event_hash,
                sequence=sequence,
            )
            await asyncio.to_thread(
                state.audit,
                action="session.append",
                principal=principal,
                session_id=context_session_id,
                result=f"sequence={sequence}",
            )
            return JSONResponse(
                content={"appended": True, "duplicate": False, "sequence": sequence}
            )

        @app.post("/internal/agents/context/sessions/commit")
        async def agent_context_session_commit(request: Request):
            owner._agent_context_auth(request)
            body = await request.json()
            principal, user = await asyncio.to_thread(owner._agent_context_identity, request, body)
            context_session_id = str(body.get("context_session_id") or "")
            state = store(request)
            session = await asyncio.to_thread(
                state.get_session,
                context_session_id,
                principal=principal,
            )
            if session is None:
                raise HTTPException(status_code=404, detail="context session not found")
            if bool(session.get("committed")):
                return JSONResponse(content={"committed": True, "duplicate": True})
            personal_scope = owner._agent_context_scopes(user)["personal_memory"]
            raw_used_refs = body.get("used_context_refs") or []
            if not isinstance(raw_used_refs, list):
                raise HTTPException(
                    status_code=400,
                    detail="used_context_refs must be a list",
                )
            usage = await owner._agent_context_submit_usage(
                state=state,
                user=user,
                personal_scope=personal_scope,
                session=session,
                principal=principal,
                ref_ids=[str(item or "") for item in raw_used_refs],
            )
            result = await owner._workspace_request(
                user,
                personal_scope,
                "POST",
                f"/api/v1/sessions/{session['openviking_session_id']}/commit",
                json={},
            )
            await asyncio.to_thread(
                state.mark_committed,
                context_session_id,
                principal=principal,
                result_hash=stable_hash(result),
            )
            await asyncio.to_thread(
                state.audit,
                action="session.commit",
                principal=principal,
                session_id=context_session_id,
            )
            return JSONResponse(
                content={
                    "committed": True,
                    "duplicate": False,
                    "result_hash": stable_hash(result),
                    "usage": usage,
                }
            )
