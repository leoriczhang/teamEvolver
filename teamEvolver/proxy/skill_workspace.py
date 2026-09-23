"""Hub-backed Skill workbench endpoints.

The workbench reads and writes the shared team skill library directly
(OpenViking, or the PostgreSQL object store when OpenViking is unavailable) —
never a local working copy. Every console surface therefore shows one skill set
with one version per skill: the same ``manifest.json`` / registry the 运行总览
status card reads.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from team_replay.lab.service import parse_skill_markdown
from team_skills.library.bundle import is_ignored_bundle_rel_path
from team_skills.library.hub import SkillFileConflictError, SkillHub

from .skills_admin import _clear_version_cache, _require_admin_request

_MAX_TEXT_BYTES = 2 * 1024 * 1024

_SOURCE_LABELS = {
    "viking": "OpenViking 团队技能",
    "postgres": "PostgreSQL 团队技能",
    "local": "本地对象存储团队技能",
}


def _eff_config(owner: Any):
    """Request-scoped effective config (tenant overrides merged)."""
    from .routes import _tenant_effective_config

    return _tenant_effective_config(owner)


def team_hub(owner: Any) -> SkillHub:
    """Team skill hub for the request's tenant (OpenViking → PostgreSQL)."""
    from ..tenants.registry import current_tenant_id

    return SkillHub.team_from_config(_eff_config(owner), tenant_id=current_tenant_id())


def _validate_rel_path(relative: str) -> str:
    value = str(relative or "").replace("\\", "/")
    if (
        not value
        or value.startswith("/")
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or is_ignored_bundle_rel_path(value)
    ):
        raise ValueError("请输入 Skill 内的相对文件路径")
    return value


def _file_payload(relative: str, data: bytes) -> dict[str, Any]:
    size = len(data)
    if size > _MAX_TEXT_BYTES:
        return {"path": relative, "size": size, "editable": False, "reason": "文件超过 2 MB，请在本地编辑"}
    try:
        content = data.decode("utf-8")
        if "\0" in content:
            raise UnicodeError()
    except UnicodeError:
        return {"path": relative, "size": size, "editable": False, "reason": "二进制文件不支持在线文本编辑"}
    return {
        "path": relative, "content": content, "size": size, "editable": True,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_workspace_file(hub: SkillHub, name: str, relative: str) -> dict[str, Any]:
    path = _validate_rel_path(relative)
    try:
        data = hub.read_skill_file(name, path)
    except FileNotFoundError as exc:
        raise FileNotFoundError("文件不存在") from exc
    return _file_payload(path, data)


def list_workspace(owner: Any) -> dict[str, Any]:
    """Skill list + version for every team skill, straight from the hub."""
    hub = team_hub(owner)
    skills: list[dict[str, Any]] = []
    for record in hub.list_remote():
        name = str(record.get("name") or "")
        if not name:
            continue
        paths = sorted(
            str((item or {}).get("path") or "")
            for item in record.get("files") or []
            if isinstance(item, dict) and str((item or {}).get("path") or "")
        )
        skills.append({
            "name": name,
            "relative_path": name,
            "description": str(record.get("description") or ""),
            "category": str(record.get("category") or "general"),
            "version": int(record.get("version") or 0),
            "files": paths,
            "file_count": len(paths),
            "updated_at": str(record.get("uploaded_at") or ""),
        })
    skills.sort(key=lambda item: item["name"].lower())
    source = hub.describe_source()
    backend = str(source.get("backend") or "")
    label = _SOURCE_LABELS.get(backend, "团队技能库")
    root = f"{label}（OpenViking 不可用回退）" if source.get("fallback") else label
    return {"name": label, "root": root, "source": source, "skills": skills}


def write_workspace_file(
    hub: SkillHub, name: str, relative: str, content: str, expected_sha256: str | None,
) -> dict[str, Any]:
    """Write one file into the hub's live bundle; returns the read payload."""
    path = _validate_rel_path(relative)
    data = content.encode("utf-8")
    if len(data) > _MAX_TEXT_BYTES:
        raise ValueError("文件不能超过 2 MB")
    if path == "SKILL.md" and parse_skill_markdown(content)["name"] != name:
        raise ValueError("SKILL.md 的 name 必须与 Skill 名称一致")
    try:
        entry = hub.write_skill_file(name, path, data, expected_sha256=expected_sha256)
    except SkillFileConflictError as exc:
        raise HTTPException(status_code=409, detail="文件已被更新，请保留当前修改并重新打开文件后合并") from exc
    return {**_file_payload(path, data), "record": entry}


def read_skill_detail(owner: Any, name: str) -> dict[str, Any]:
    """One team skill's SKILL.md + metadata, read from the hub."""
    hub = team_hub(owner)
    record = next(
        (item for item in hub.list_remote() if str(item.get("name") or "") == name),
        None,
    )
    if record is None:
        raise FileNotFoundError(f"skill not found: {name}")
    raw_md = hub.read_skill_file(name, "SKILL.md").decode("utf-8", errors="replace")
    parsed = parse_skill_markdown(raw_md)
    files = sorted(
        str((item or {}).get("path") or "")
        for item in record.get("files") or []
        if isinstance(item, dict) and str((item or {}).get("path") or "")
    )
    return {
        "name": name,
        "category": str(parsed.get("category") or record.get("category") or "general"),
        "description": str(parsed.get("description") or record.get("description") or ""),
        "body": str(parsed.get("content") or ""),
        "skill_md": raw_md,
        "files": files,
        "file_count": len(files),
        "version": int(record.get("version") or 0),
        "updated_at": str(record.get("uploaded_at") or ""),
    }


def _enqueue_delivery(owner: Any, name: str, record: dict[str, Any]) -> dict[str, Any]:
    """Queue the published version for Agent delivery; never raises."""
    try:
        from team_skills.library.mutations import SkillMutationService

        commit = SkillMutationService.from_config(
            _eff_config(owner), tenant_id=_current_tenant()
        ).record_committed(
            action="update",
            mutation_id=f"workbench-{uuid.uuid4().hex}",
            expected=record,
            tenant_ids=[],
            metadata={"origin": "workbench"},
        )
        return {
            "synced": True,
            "action": "update",
            "event_id": commit.get("event_id") or "",
            "version": int(record.get("version") or 0),
        }
    except Exception as exc:  # noqa: BLE001 - delivery is advisory for the editor
        return {"synced": False, "reason": str(exc)}


def _current_tenant() -> str:
    from ..tenants.registry import current_tenant_id

    return current_tenant_id()


def _invalidate_status_cache() -> None:
    """Drop the cached /status payload so 运行总览 shows the new version too."""
    try:
        from .routes import _invalidate_dashboard_cache

        _invalidate_dashboard_cache("status")
    except Exception:  # noqa: BLE001 - cache hygiene must never fail a save
        pass


def register_skill_workspace_routes(owner: Any, app: FastAPI) -> None:
    @app.get("/api/replay-lab/workspace")
    async def workspace_index():
        try:
            return await asyncio.to_thread(list_workspace, owner)
        except Exception as exc:  # noqa: BLE001 - surfaces storage outages to the UI
            raise HTTPException(status_code=502, detail=f"读取团队技能库失败: {exc}") from exc

    @app.get("/api/replay-lab/skills")
    async def lab_skill_index():
        """Skill list for the embedded lab — same hub source as 运行总览."""
        try:
            index = await asyncio.to_thread(list_workspace, owner)
        except Exception as exc:  # noqa: BLE001 - surfaces storage outages to the UI
            raise HTTPException(status_code=502, detail=f"读取团队技能库失败: {exc}") from exc
        return {"sharing_enabled": index["source"]["backend"] != "local", "skills": index["skills"]}

    @app.get("/api/replay-lab/skills/{name}")
    async def lab_skill_detail(name: str):
        try:
            return await asyncio.to_thread(read_skill_detail, owner, name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaces storage outages to the UI
            raise HTTPException(status_code=502, detail=f"读取团队技能库失败: {exc}") from exc

    @app.get("/api/replay-lab/workspace/{name}/file")
    async def workspace_read(name: str, path: str):
        try:
            hub = await asyncio.to_thread(team_hub, owner)
            return await asyncio.to_thread(read_workspace_file, hub, name, path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="代码文件不存在") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/replay-lab/workspace/{name}/file")
    async def workspace_write(name: str, body: dict[str, Any], request: Request):
        _require_admin_request(request)
        content = body.get("content")
        if not isinstance(content, str) or "expected_sha256" not in body:
            raise HTTPException(status_code=400, detail="content 和 expected_sha256 为必填")
        expected = body["expected_sha256"]
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=400, detail="expected_sha256 必须是字符串或 null")
        try:
            hub = await asyncio.to_thread(team_hub, owner)
            result = await asyncio.to_thread(
                write_workspace_file, hub, name, str(body.get("path") or ""), content, expected,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Skill 不存在") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record = dict(result.pop("record") or {})
        cloud = await asyncio.to_thread(_enqueue_delivery, owner, name, record)
        _clear_version_cache(name)
        _invalidate_status_cache()
        return {
            **result,
            "version": int(record.get("version") or 0),
            "cloud": cloud,
        }