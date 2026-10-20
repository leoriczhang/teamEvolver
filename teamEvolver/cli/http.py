"""Short authenticated API requests shared by operational commands."""
import json
import os

import click
import httpx

from ..bootstrap import load_bootstrap


def api_request(ctx, method, path, body=None):
    config = load_bootstrap()
    url = ctx.obj.get("url") or f"http://127.0.0.1:{config.proxy_port}"
    key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")
    if not key:
        raise click.ClickException("TEAMEVOLVER_ROOT_API_KEY is required")
    try:
        response = httpx.request(method, url + path, json=body, timeout=20,
                                 headers={"Authorization": f"Bearer {key}", "X-Tenant-Id": ctx.obj["tenant"]})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException("API request failed") from exc
    click.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return response.json()
