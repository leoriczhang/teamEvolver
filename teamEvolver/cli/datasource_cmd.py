"""Datasource commands use the same authenticated interface as the console."""
import json
import os

import click
import httpx

from ..config_store import ConfigStore


@click.group()
@click.option("--tenant", default="default", help="Account ID")
@click.option("--url", default="", help="teamEvolver service URL")
@click.pass_context
def datasource(ctx, tenant, url):
    """Tenant adapter status, preview and pull."""
    store = ConfigStore()
    ctx.obj = {
        "tenant": tenant,
        "url": url or f"http://127.0.0.1:{store.get('service.port') or 52010}",
    }


def request(ctx, method, suffix="", body=None):
    key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY") or os.environ.get("EVOLVE_INGEST_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}", "X-Tenant-Id": ctx.obj["tenant"]}
    try:
        response = httpx.request(method, ctx.obj["url"] + "/api/datasource" + suffix,
                                headers=headers, json=body, timeout=3600)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(response.json(), ensure_ascii=False, indent=2))


@datasource.command()
@click.pass_context
def status(ctx):
    request(ctx, "GET")


@datasource.command()
@click.pass_context
def test(ctx):
    request(ctx, "POST", "/test")


@datasource.command()
@click.argument("filename")
@click.pass_context
def bind(ctx, filename):
    request(ctx, "PUT", body={"file": filename})


def filter_options(fn):
    for name in ("from_timestamp", "to_timestamp", "session_id", "user_id"):
        fn = click.option("--" + name.replace("_", "-"), default="")(fn)
    return click.option("--max-sessions", default=100, type=click.IntRange(1, 1000))(fn)


@datasource.command(name="list")
@filter_options
@click.pass_context
def list_sessions(ctx, **filters):
    request(ctx, "POST", "/sessions", {k: v for k, v in filters.items() if v != ""})


@datasource.command()
@filter_options
@click.option("--force-reprocess", is_flag=True)
@click.option("--defer-evolution-trigger", is_flag=True)
@click.pass_context
def pull(ctx, **filters):
    request(ctx, "POST", "/pull", {k: v for k, v in filters.items() if v != ""})
