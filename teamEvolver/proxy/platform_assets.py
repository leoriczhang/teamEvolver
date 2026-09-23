"""Read-only platform artifact browser backed by PostgreSQL or NAS."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..storage import build_object_store, is_not_found_error, normalize_backend
from ..tenants.registry import current_tenant_id, tenant_scope_prefix
from .users_admin import _request_user

_MAX_TREE_NODES = 10_000
_MAX_CONTENT_BYTES = 4 * 1024 * 1024
_ASSET_URI_PREFIX = "platform://"

_SESSION_FAMILIES = {
    "sessions": "待消费 Session 队列",
    "session_archive": "Session 永久归档",
    "session_filter_audit": "Session 过滤决策审计",
    "session_ledger": "Session 生命周期总账",
    "session_index.json": "Session 元信息索引",
}

_SKILL_FAMILIES = {
    "skill_lab": "Skill 实验数据与运行结果",
    "skill_datasets": "Skill Test Dataset",
    "evolution_datasets": "Evolution 合成数据集",
    "skill_evidence": "Skill 效果 Evidence",
    "experience_library": "Skill 使用经验",
    "skill_version_context": "Skill 版本上下文",
    "skill_mutation_commits": "Skill 变更提交记录",
    "skill_sync_outbox": "Skill 分发发件箱",
    "skill_tombstones": "Skill 删除标记",
    "candidate_skills": "Skill Candidate",
    "validation_jobs": "True Replay 验证任务",
    "validation_claims": "验证任务认领记录",
    "validation_results": "独立验证结果",
    "validation_evaluations": "验证聚合评估",
    "validation_decisions": "Candidate Review 裁决",
    "validation_decision_index.json": "验证裁决索引",
    "validation_open_jobs.json": "开放验证任务索引",
    "human_review": "人工复核任务",
    "memory-changes": "Memory Change 总账",
    "memory-replays": "Memory True Replay 记录",
    "manifest.json": "Skill 清单索引",
    "evolve_skill_registry.json": "Skill ID 注册表",
}


@dataclass(frozen=True)
class _PlatformAssetSource:
    source_id: str
    label: str
    description: str
    backend: str
    configured_backend: str
    bucket: Any
    key_prefix: str
    families: dict[str, str]

    @property
    def backend_label(self) -> str:
        return "PostgreSQL" if self.backend == "postgres" else "NAS / 本地存储"

    @property
    def root_uri(self) -> str:
        return f"{_ASSET_URI_PREFIX}{self.source_id}"


def _configured_backend(config: Any, purpose: str) -> tuple[str, str]:
    if purpose == "session" and bool(getattr(config, "storage_pg_enabled", False)):
        return "postgres", "postgres"
    field = (
        "sharing_session_backend"
        if purpose == "session"
        else "sharing_skill_backend"
    )
    configured = normalize_backend(str(getattr(config, field, "") or "")) or "local"
    # Platform artifacts are service-owned state. OpenViking may remain an
    # Agent-facing mirror, but this browser must never depend on it.
    effective = configured if configured == "postgres" else "local"
    return configured, effective


def _build_platform_bucket(config: Any, purpose: str, tenant_id: str):
    configured, backend = _configured_backend(config, purpose)
    shared_root = str(getattr(config, "sharing_local_root", "") or "")
    local_root = (
        str(getattr(config, "sharing_skill_local_root", "") or "") or shared_root
        if purpose == "skill"
        else shared_root
    )
    bucket = build_object_store(
        backend=backend,
        local_root=local_root,
        pg_dsn=str(getattr(config, "storage_pg_dsn", "") or ""),
        pg_schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
        pg_pool_min=int(getattr(config, "storage_pg_pool_min", 2) or 2),
        pg_pool_max=int(getattr(config, "storage_pg_pool_max", 20) or 20),
        pg_command_timeout=float(
            getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0
        ),
        pg_ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
        tenant_id=tenant_id,
    )
    return configured, backend, bucket


def _platform_sources(config: Any, tenant_id: str) -> list[_PlatformAssetSource]:
    session_configured, session_backend, session_bucket = _build_platform_bucket(
        config, "session", tenant_id
    )
    skill_configured, skill_backend, skill_bucket = _build_platform_bucket(
        config, "skill", tenant_id
    )
    skill_prefix = tenant_scope_prefix(
        tenant_id,
        is_pg=skill_backend == "postgres",
    )
    return [
        _PlatformAssetSource(
            source_id="session",
            label="Session 流水",
            description="Session 队列、归档、过滤审计与生命周期索引",
            backend=session_backend,
            configured_backend=session_configured,
            bucket=session_bucket,
            key_prefix="",
            families=_SESSION_FAMILIES,
        ),
        _PlatformAssetSource(
            source_id="skill",
            label="Skill 进化产物",
            description="Candidate、Evidence、True Replay、版本与发布中间产物",
            backend=skill_backend,
            configured_backend=skill_configured,
            bucket=skill_bucket,
            key_prefix=skill_prefix,
            families=_SKILL_FAMILIES,
        ),
    ]


def _relative_key(source: _PlatformAssetSource, physical_key: str) -> str:
    key = str(physical_key or "").replace("\\", "/").lstrip("/")
    if source.key_prefix:
        if not key.startswith(source.key_prefix):
            return ""
        key = key[len(source.key_prefix) :]
    return key.strip("/")


def _allowed_key(source: _PlatformAssetSource, relative_key: str) -> bool:
    top = str(relative_key or "").split("/", 1)[0]
    return bool(top and top in source.families)


def _asset_uri(source: _PlatformAssetSource, relative_key: str = "") -> str:
    clean = str(relative_key or "").strip("/")
    return f"{source.root_uri}/{clean}" if clean else source.root_uri


def _collect_source_entries(
    source: _PlatformAssetSource,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    keys: list[str] = []
    truncated = False
    for item in source.bucket.iter_objects(prefix=source.key_prefix):
        relative = _relative_key(source, str(getattr(item, "key", "") or ""))
        if not relative or not _allowed_key(source, relative):
            continue
        keys.append(relative)
        if len(keys) >= _MAX_TREE_NODES:
            truncated = True
            break

    directories: set[str] = set()
    family_counts: dict[str, int] = {}
    for key in keys:
        top = key.split("/", 1)[0]
        family_counts[top] = family_counts.get(top, 0) + 1
        parts = key.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))

    entries: list[dict[str, Any]] = []
    for directory in sorted(directories):
        top = directory.split("/", 1)[0]
        entries.append(
            {
                "uri": _asset_uri(source, directory),
                "key": directory,
                "name": directory.rsplit("/", 1)[-1],
                "is_dir": True,
                "source_id": source.source_id,
                "backend": source.backend,
                "relative_path": directory,
                "purpose": source.families.get(top, ""),
            }
        )
    for key in sorted(keys):
        top = key.split("/", 1)[0]
        entries.append(
            {
                "uri": _asset_uri(source, key),
                "key": key,
                "name": key.rsplit("/", 1)[-1],
                "is_dir": False,
                "source_id": source.source_id,
                "backend": source.backend,
                "relative_path": key,
                "purpose": source.families.get(top, ""),
            }
        )
    entries.sort(
        key=lambda item: (
            item["relative_path"].count("/"),
            not item["is_dir"],
            item["relative_path"].lower(),
        )
    )
    return entries, family_counts, truncated


def _source_payload(
    source: _PlatformAssetSource,
    *,
    object_count: int = 0,
    family_counts: dict[str, int] | None = None,
    truncated: bool = False,
    error: str = "",
) -> dict[str, Any]:
    counts = family_counts or {}
    return {
        "id": source.source_id,
        "label": source.label,
        "description": source.description,
        "backend": source.backend,
        "backend_label": source.backend_label,
        "configured_backend": source.configured_backend,
        "root_uri": source.root_uri,
        "object_count": object_count,
        "family_count": len(counts),
        "truncated": truncated,
        "error": error,
        "families": [
            {
                "key": key,
                "label": label,
                "object_count": counts.get(key, 0),
            }
            for key, label in source.families.items()
            if counts.get(key, 0)
        ],
    }


def _parse_asset_uri(
    sources: list[_PlatformAssetSource],
    uri: str,
) -> tuple[_PlatformAssetSource, str]:
    value = str(uri or "").strip().rstrip("/")
    if not value.startswith(_ASSET_URI_PREFIX):
        raise HTTPException(status_code=400, detail="invalid platform asset URI")
    remainder = value[len(_ASSET_URI_PREFIX) :]
    source_id, separator, relative = remainder.partition("/")
    if not separator or not relative:
        raise HTTPException(status_code=400, detail="select a platform asset file")
    source = next((item for item in sources if item.source_id == source_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="platform asset source not found")
    clean = str(PurePosixPath(relative))
    if clean in {"", "."} or clean.startswith("../") or "/../" in f"/{clean}/":
        raise HTTPException(status_code=400, detail="invalid platform asset path")
    if not _allowed_key(source, clean):
        raise HTTPException(status_code=403, detail="platform asset path is not exposed")
    return source, clean


class PlatformAssetsMixin:
    """Expose service-owned PostgreSQL/NAS artifacts without OpenViking."""

    def _platform_assets_config(self):
        from ..tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), self.config)

    def _register_platform_assets_routes(self, app: FastAPI) -> None:
        owner = self

        @app.get("/api/platform-assets/tree")
        async def platform_assets_tree(request: Request):
            _request_user(request)
            tenant_id = current_tenant_id()
            config = owner._platform_assets_config()
            try:
                sources = _platform_sources(config, tenant_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"platform storage is unavailable: {exc}",
                ) from exc

            async def collect(source: _PlatformAssetSource):
                try:
                    entries, family_counts, truncated = await asyncio.to_thread(
                        _collect_source_entries,
                        source,
                    )
                    payload = _source_payload(
                        source,
                        object_count=sum(family_counts.values()),
                        family_counts=family_counts,
                        truncated=truncated,
                    )
                    return entries, payload
                except Exception as exc:  # noqa: BLE001
                    return [], _source_payload(source, error=str(exc))

            collected = await asyncio.gather(*(collect(source) for source in sources))
            entries = [entry for source_entries, _ in collected for entry in source_entries]
            return JSONResponse(
                content={
                    "tenant_id": tenant_id,
                    "read_only": True,
                    "entries": entries,
                    "sources": [payload for _, payload in collected],
                }
            )

        @app.get("/api/platform-assets/content")
        async def platform_asset_content(
            request: Request,
            uri: str = Query(...),
        ):
            _request_user(request)
            tenant_id = current_tenant_id()
            config = owner._platform_assets_config()
            try:
                sources = _platform_sources(config, tenant_id)
                source, relative_key = _parse_asset_uri(sources, uri)
                physical_key = f"{source.key_prefix}{relative_key}"
                body = await asyncio.to_thread(
                    lambda: source.bucket.get_object(physical_key).read()
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                if is_not_found_error(exc):
                    raise HTTPException(
                        status_code=404,
                        detail="platform asset not found",
                    ) from exc
                raise HTTPException(
                    status_code=503,
                    detail=f"platform storage is unavailable: {exc}",
                ) from exc
            if len(body) > _MAX_CONTENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="platform asset preview is limited to 4 MB",
                )
            try:
                content = body.decode("utf-8")
                is_text = True
            except UnicodeDecodeError:
                content = ""
                is_text = False
            top = relative_key.split("/", 1)[0]
            return JSONResponse(
                content={
                    "uri": _asset_uri(source, relative_key),
                    "key": relative_key,
                    "name": relative_key.rsplit("/", 1)[-1],
                    "source_id": source.source_id,
                    "source_label": source.label,
                    "backend": source.backend,
                    "backend_label": source.backend_label,
                    "purpose": source.families.get(top, ""),
                    "size": len(body),
                    "is_text": is_text,
                    "content": content,
                }
            )
