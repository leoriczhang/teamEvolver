"""Inspect validation using the authenticated asynchronous API."""
import click

from .http import api_request


@click.group()
@click.option("--tenant", required=True)
@click.option("--url", default="")
@click.pass_context
def validation(ctx, tenant, url):
    """Tenant validation state and queued work."""
    ctx.obj = {"tenant": tenant, "url": url}


@validation.command("status")
@click.pass_context
def validation_status(ctx):
    api_request(ctx, "GET", "/api/skill-candidates?scope=open")
