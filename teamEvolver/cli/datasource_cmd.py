"""Datasource commands use the same authenticated interface as the console."""

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import click
import httpx

from ..bootstrap import load_bootstrap


@click.group()
@click.option("--tenant", default="default", help="Account ID")
@click.option("--url", default="", help="teamEvolver service URL")
@click.pass_context
def datasource(ctx, tenant, url):
    """Tenant adapter status, preview and pull."""
    config = load_bootstrap()
    ctx.obj = {
        "tenant": tenant,
        "url": url or f"http://127.0.0.1:{config.proxy_port}",
    }


def _request_json(ctx, method, suffix="", body=None):
    key = os.environ.get("TEAMEVOLVER_ROOT_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}", "X-Tenant-Id": ctx.obj["tenant"]}
    try:
        response = httpx.request(
            method, ctx.obj["url"] + "/api/datasource" + suffix, headers=headers, json=body, timeout=20
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException(str(exc)) from exc
    return response.json()


def request(ctx, method, suffix="", body=None):
    click.echo(json.dumps(_request_json(ctx, method, suffix, body), ensure_ascii=False, indent=2))


@datasource.command()
@click.pass_context
def status(ctx):
    request(ctx, "GET")


def connection_path(connection_id):
    return "/connections/" + quote(connection_id, safe="") if connection_id else ""


@datasource.command()
@click.option("--connection", default="")
@click.pass_context
def test(ctx, connection):
    request(ctx, "POST", connection_path(connection) + "/test", {})


@datasource.command(name="connections")
@click.pass_context
def list_connections(ctx):
    """List the current tenant's named connections and pinned revisions."""
    request(ctx, "GET", "/connections")


@datasource.command(name="save-connection")
@click.argument("connection_id")
@click.argument("config_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def save_connection(ctx, connection_id, config_file):
    """Save a named connection using JSON with its expected_revision."""
    try:
        body = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or type(body.get("expected_revision")) is not int:
            raise ValueError("expected a JSON object with expected_revision")
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    request(ctx, "PUT", connection_path(connection_id), body)


@datasource.command()
@click.argument("filename")
@click.pass_context
def bind(ctx, filename):
    request(ctx, "PUT", body={"file": filename})


def filter_options(fn):
    for name in ("from_timestamp", "to_timestamp", "session_id", "user_id", "agent_id"):
        fn = click.option("--" + name.replace("_", "-"), default="")(fn)
    return fn


@datasource.command(name="list")
@filter_options
@click.option("--connection", default="")
@click.option("--max-sessions", default=100, type=click.IntRange(1, 1000))
@click.pass_context
def list_sessions(ctx, connection, **filters):
    limit = filters.pop("max_sessions")
    request(
        ctx,
        "POST",
        connection_path(connection) + "/sessions",
        {"filters": {k: v for k, v in filters.items() if v}, "max_sessions": limit},
    )


@datasource.command()
@filter_options
@click.option("--connection", default="")
@click.pass_context
def pull(ctx, connection, **filters):
    timestamps = {k: filters.pop(k) for k in ("from_timestamp", "to_timestamp") if filters.get(k)}
    request(
        ctx,
        "POST",
        connection_path(connection) + "/pull",
        {**timestamps, "filters": {k: v for k, v in filters.items() if v}},
    )


@datasource.command()
@click.argument("job_id")
@click.option("--interval", type=click.IntRange(1, 300), default=3, show_default=True)
@click.option("--timeout", type=click.IntRange(min=1), default=1800, show_default=True)
@click.pass_context
def watch(ctx, job_id, interval, timeout):
    """Wait for one pull's source Sessions and their descendants to finish."""
    deadline = time.monotonic() + timeout
    endpoint = "/jobs/" + quote(job_id, safe="") + "/progress?limit=1"
    previous = None
    while True:
        result = _request_json(ctx, "GET", endpoint)
        summary = {key: result[key] for key in ("job", "status", "counts", "summary")}
        if summary != previous:
            click.echo(json.dumps(summary, ensure_ascii=False))
            previous = summary
        if result["status"] == "completed" and result["complete"]:
            return
        if result["complete"] or not result["tracking_available"] or result["status"] == "incomplete":
            raise click.ClickException(f"Pull {job_id}: {result['status']}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise click.ClickException(f"Timed out watching {job_id}. Resume with the same job ID.")
        time.sleep(min(interval, remaining))
