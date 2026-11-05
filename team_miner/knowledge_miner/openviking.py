"""Miner knowledge operations over the single platform Compile/HTTP client."""

from __future__ import annotations

from teamEvolver.openviking import (
    OpenVikingClient,
    OpenVikingError,
    ensure_directories,
)
from teamEvolver.openviking.compile import CompileClient

from .default_skill import DEFAULT_LLM_WIKI_SKILL_BODY

DEFAULT_LLM_WIKI_SKILL_URI = "viking://agent/skills/llm-wiki"


async def clients(services, settings):
    factory = services.extras.get("miner_client_factory")
    cfg = services.extras.get("config")
    if factory:
        client = factory(settings)
    elif services.extras.get("compile_client_factory"):
        client = await services.extras["compile_client_factory"](settings)
    else:
        client = OpenVikingClient(
            endpoint=cfg.sharing_viking_endpoint, api_key=cfg.sharing_viking_api_key,
            account_id=settings["account_id"], user=settings["user_id"],
        )
    return client, CompileClient(client)


async def request(client, method, path, *, body=None, params=None, key="", shared_admin=False):
    result = await client.request(
        method,
        path,
        json_body=body,
        params=params,
        idempotency_key=key,
        shared_admin=shared_admin,
    )
    if not result.ok:
        raise OpenVikingError(f"OpenViking HTTP {result.status_code}", status_code=result.status_code)
    return result.data


async def ensure_workspace(client, settings):
    """Create configured Miner roots and every missing parent idempotently."""
    return await ensure_directories(
        client,
        [settings["source_uri"], settings["target_uri"]],
    )


async def ensure_mining_skill(client, compiler, skill_uri, key):
    """Install the built-in mining Skill when its default URI is missing."""
    name = str(skill_uri or "").rstrip("/").rsplit("/", 1)[-1]
    current = await compiler.get_skill(
        skill_name=name,
        params={
            "target_uri": "viking://agent/skills",
            "include_content": "true",
            "include_integrity": "true",
        },
        shared_admin=True,
    )
    if not current.ok and current.status_code == 404:
        if skill_uri != DEFAULT_LLM_WIKI_SKILL_URI:
            raise ValueError("configured Miner Skill is not installed")
        await request(
            client,
            "POST",
            "/api/v1/skills",
            body={
                "data": DEFAULT_LLM_WIKI_SKILL_BODY,
                "wait": True,
                "target_uri": "viking://agent/skills",
            },
            key=key,
            shared_admin=True,
        )
        current = await compiler.get_skill(
            skill_name=name,
            params={
                "target_uri": "viking://agent/skills",
                "include_content": "true",
                "include_integrity": "true",
            },
            shared_admin=True,
        )
    if not current.ok:
        raise OpenVikingError(
            "Miner Skill unavailable",
            status_code=current.status_code,
        )
    return current


async def documents(client, root):
    tree = await request(client, "GET", "/api/v1/fs/tree",
                         params={"uri": root, "node_limit": 10000, "level_limit": 20, "output": "original"})
    entries = tree.get("entries", tree.get("items", [])) if isinstance(tree, dict) else tree
    if not isinstance(entries, list) or len(entries) >= 10000:
        raise ValueError("Wiki inventory incomplete or exceeds node limit")
    from .schemas import uri
    pages = {}
    total = 0
    for entry in entries:
        if entry.get("isDir", entry.get("is_dir")) or not str(entry.get("uri", "")).endswith(".md"):
            continue
        path = uri(entry["uri"], root, allow_root=False)
        content = await request(client, "GET", "/api/v1/content/read", params={"uri": path})
        if not isinstance(content, str):
            raise ValueError("Wiki content must be text")
        total += len(content.encode())
        if len(pages) >= 1000 or total > 16 * 1024 * 1024:
            raise ValueError("Wiki exceeds graph budget")
        pages[path] = content
    return pages
