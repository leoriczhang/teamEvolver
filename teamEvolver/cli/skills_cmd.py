"""Read Skills and materialize a rebuildable cache from the authoritative API."""
import base64
import json
import os

import click
import httpx

from ..bootstrap import load_bootstrap


@click.group()
@click.option("--tenant", required=True)
@click.option("--url", default="")
@click.pass_context
def skills(ctx, tenant, url):
    """Inspect the selected tenant Skill library."""
    config = load_bootstrap()
    ctx.obj = {"tenant": tenant, "url": url or f"http://127.0.0.1:{config.proxy_port}", "config": config}


@skills.command("list")
@click.pass_context
def skills_list(ctx):
    from .http import api_request
    api_request(ctx, "GET", "/api/skills")


@skills.command("pull")
@click.option("--user-id", required=True)
@click.pass_context
def skills_pull(ctx, user_id):
    from team_skills.library.cache import SkillRuntimeCache
    token = os.environ.get("TEAMEVOLVER_TENANT_TOKEN", "")
    if not token:
        raise click.ClickException("TEAMEVOLVER_TENANT_TOKEN is required")
    response = httpx.get(ctx.obj["url"] + "/sync/skills", params={"user_id": user_id},
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
    response.raise_for_status()
    cache = SkillRuntimeCache(ctx.obj["config"].skills_runtime_cache_dir)
    result = []
    for skill in response.json()["skills"]:
        bundle = {f["path"]: base64.b64decode(f["content_b64"], validate=True) for f in skill["files"]}
        result.append(cache.materialize(ctx.obj["tenant"], skill["name"], bundle))
    click.echo(json.dumps(result))
